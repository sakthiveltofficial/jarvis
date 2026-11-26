// Jarvis particle orb: a GPU particle cloud that morphs with what Jarvis is doing and feeling.
//
//   state  -> shape   idle: breathing sphere, listening: ring leaning toward you, thinking: swirling vortex,
//                     speaking: sphere that ripples with the voice, acting: an icon for the current tool
//   mood   -> colour, speed and texture (Jarvis's own <mood> tag first, then how the user sounds)
//   audio  -> Jarvis's voice pushes particles outward in waves; the user's voice draws them in
//
// Classic script (no top-level import) so `node --check` in CI can parse it; Three.js is loaded on demand.
// Public API: window.JarvisOrb.start(container, {quality, theme, character}) -> Promise<controller | null>.
(() => {
  "use strict";

  // ------------------------------------------------------------------ palettes
  // One palette per character (Settings > Behaviour > Character): Jarvis lives in blues, Ultron in reds.
  // [colour A, colour B, sparkle, speed, turbulence, sparkle amount]
  const PALETTES = {
    jarvis: {
      neutral:    ["#3d8bff", "#7fd4ff", "#ffffff", 0.8, 0.6, 0.06],
      listening:  ["#38d9ff", "#8af0ff", "#ffffff", 1.0, 0.7, 0.08],
      thinking:   ["#6b6bff", "#a98bff", "#ffffff", 1.35, 0.9, 0.12],
      // the companion's mood (from the <mood> tag on each reply)
      calm:       ["#4f9dff", "#9ad7ff", "#ffffff", 0.6, 0.5, 0.05],
      happy:      ["#4fc3ff", "#bfe9ff", "#fff6d8", 1.0, 0.8, 0.12],
      excited:    ["#00e5ff", "#5d8bff", "#e8fbff", 1.6, 1.2, 0.28],
      curious:    ["#3ee6c1", "#7fd4ff", "#ffffff", 1.05, 0.85, 0.12],
      focused:    ["#2f6bff", "#9fb4ff", "#ffffff", 0.8, 0.4, 0.06],
      concerned:  ["#2ec4b6", "#9be7e0", "#ffffff", 0.55, 0.45, 0.04],
      apologetic: ["#8f9dff", "#c8d2ff", "#ffffff", 0.5, 0.45, 0.04],
      playful:    ["#5ad1ff", "#b38bff", "#ffffff", 1.4, 1.0, 0.22],
      // how the user sounds (from the emotion detector)
      u_stressed: ["#3cc9b5", "#a6f0e4", "#ffffff", 0.4, 0.3, 0.03],
      u_excited:  ["#22d3ff", "#6aa8ff", "#e8fbff", 1.4, 1.1, 0.24],
      u_down:     ["#7aa2ff", "#c7d6ff", "#ffffff", 0.5, 0.45, 0.05],
      u_tired:    ["#8ea6ff", "#c7b8ff", "#ffffff", 0.45, 0.4, 0.04],
    },
    ultron: {        // hotter and a little more restless than Jarvis at every mood
      neutral:    ["#ff2a2a", "#ff7a4d", "#ffd0c0", 0.85, 0.7, 0.08],
      listening:  ["#ff3b3b", "#ff9a6b", "#ffe0d0", 1.05, 0.8, 0.1],
      thinking:   ["#b0002a", "#ff3d6e", "#ffd0d8", 1.45, 1.0, 0.14],
      calm:       ["#c4161c", "#ff5a4a", "#ffd0c0", 0.6, 0.5, 0.05],
      happy:      ["#ff4d2e", "#ffa040", "#fff0d0", 1.0, 0.85, 0.14],
      excited:    ["#ff1a1a", "#ff8c1a", "#fff2b0", 1.7, 1.3, 0.32],
      curious:    ["#ff3d6e", "#ff9a8a", "#ffffff", 1.05, 0.85, 0.12],
      focused:    ["#d10a1a", "#ff4040", "#ffd0c0", 0.8, 0.45, 0.06],
      concerned:  ["#a8102a", "#ff6070", "#ffd8dc", 0.55, 0.45, 0.04],
      apologetic: ["#9a1838", "#ff7a9a", "#ffe0e8", 0.5, 0.45, 0.04],
      playful:    ["#ff2a6a", "#ff8a3d", "#ffffff", 1.4, 1.1, 0.24],
      u_stressed: ["#b31b2e", "#ff8080", "#ffe0e0", 0.4, 0.3, 0.03],
      u_excited:  ["#ff2a1a", "#ff9a3d", "#fff2b0", 1.5, 1.2, 0.26],
      u_down:     ["#8a1030", "#ff6a80", "#ffe0e8", 0.5, 0.45, 0.05],
      u_tired:    ["#7a1a2a", "#c84a5a", "#ffd8dc", 0.45, 0.4, 0.04],
    },
  };
  const palette = (c) => PALETTES[c] || PALETTES.jarvis;
  const MOODS = PALETTES.jarvis;   // mood names are the same in every palette

  // tool name -> icon shape shown while Jarvis works
  function taskShape(tool) {
    const t = String(tool || "");
    if (t === "phone") return "phone";
    if (/schedule|remind/.test(t)) return "clock";
    if (/^(memory_search|remember)$/.test(t)) return "nodes";
    if (/^(browser|weather|open_url)$/.test(t) || t.startsWith("mcp_")) return "globe";
    return "gear";
  }
  const STATE_SHAPE = { idle: "sphere", listening: "ring", transcribing: "ring", thinking: "vortex", speaking: "sphere", acting: "gear" };

  // ------------------------------------------------------------------ shapes (unit-ish radius, Float32Array xyz)
  const rnd = (a = 1) => (Math.random() * 2 - 1) * a;
  const gauss = () => { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(6.2832 * v); };
  function rotX(p, a) { const c = Math.cos(a), s = Math.sin(a); for (let i = 0; i < p.length; i += 3) { const y = p[i + 1], z = p[i + 2]; p[i + 1] = y * c - z * s; p[i + 2] = y * s + z * c; } return p; }
  function fill(n, fn) { const p = new Float32Array(n * 3); for (let i = 0; i < n; i++) { const v = fn(i, n); p[i * 3] = v[0]; p[i * 3 + 1] = v[1]; p[i * 3 + 2] = v[2]; } return p; }
  // pick a point along a list of segments, weighted by length
  function onSegments(segs) {
    const lens = segs.map(([a, b]) => Math.hypot(b[0] - a[0], b[1] - a[1], b[2] - a[2]));
    const total = lens.reduce((x, y) => x + y, 0);
    return () => {
      let r = Math.random() * total, k = 0;
      while (r > lens[k] && k < lens.length - 1) r -= lens[k++];
      const [a, b] = segs[k], f = Math.random();
      return [a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f];
    };
  }
  const arc = (cx, cy, r, a0, a1, z = 0) => { const s = 2 * Math.PI * r * Math.abs(a1 - a0) / 6.2832; const t = a0 + (a1 - a0) * Math.random(); return [cx + Math.cos(t) * r, cy + Math.sin(t) * r, z, s]; };
  const jit = (v, a) => [v[0] + rnd(a), v[1] + rnd(a), v[2] + rnd(a)];

  // a hand (or two) from 21 MediaPipe points each: bones as thick strokes, the palm filled in
  const BONES = [[0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12],
                 [9, 13], [13, 14], [14, 15], [15, 16], [13, 17], [17, 18], [18, 19], [19, 20], [0, 17]];
  function handShape(hands) {
    return (n) => {
      // image space (0..1, y down, already mirrored) -> centred, y up, fitting a radius of ~1.1
      const pts = hands.flat();
      const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
      const cx = (Math.min(...xs) + Math.max(...xs)) / 2, cy = (Math.min(...ys) + Math.max(...ys)) / 2;
      const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 1e-3);
      const k = 2.2 / span;
      const P = hands.map((h) => h.map((p) => [(p[0] - cx) * k, -(p[1] - cy) * k, -(p[2] || 0) * k * 0.6]));
      const segs = [], palms = [];
      for (const h of P) {
        for (const [a, b] of BONES) segs.push([h[a], h[b]]);
        palms.push([h[0], h[5], h[17]], [h[5], h[9], h[17]], [h[9], h[13], h[17]]);
      }
      const line = onSegments(segs);
      const width = 0.09 * Math.min(1.4, 1 / Math.max(0.6, hands.length * 0.75));
      return fill(n, () => {
        if (Math.random() < 0.72) return jit(line(), width);
        const [a, b, c] = palms[(Math.random() * palms.length) | 0];
        let u = Math.random(), v = Math.random(); if (u + v > 1) { u = 1 - u; v = 1 - v; }
        return jit([a[0] + (b[0] - a[0]) * u + (c[0] - a[0]) * v, a[1] + (b[1] - a[1]) * u + (c[1] - a[1]) * v,
                    a[2] + (b[2] - a[2]) * u + (c[2] - a[2]) * v], 0.03);
      });
    };
  }

  const SHAPES = {
    sphere: (n) => fill(n, (i) => {
      const y = 1 - (2 * (i + 0.5)) / n, r = Math.sqrt(1 - y * y), th = i * 2.39996323;
      const k = 0.97 + Math.random() * 0.06;
      return [Math.cos(th) * r * k, y * k, Math.sin(th) * r * k];
    }),
    ring: (n) => rotX(fill(n, () => {
      const t = Math.random() * 6.2832, a = Math.random() * 6.2832, tube = 0.05 + 0.06 * Math.abs(gauss()) * 0.5;
      const R = 1.05 + Math.cos(a) * tube;
      return [Math.cos(t) * R, Math.sin(t) * R, Math.sin(a) * tube * 1.6];
    }), 0.32),
    vortex: (n) => rotX(fill(n, () => {
      const r = Math.pow(Math.random(), 0.7) * 1.3, arm = Math.floor(Math.random() * 3);
      const t = r * 4.2 + arm * 2.0944 + gauss() * 0.22 * (1.2 - r);
      return [Math.cos(t) * r, gauss() * 0.07 * (1.3 - r) + 0.02 * rnd(), Math.sin(t) * r];
    }), 1.05),
    phone: (n) => {
      const w = 0.62, h = 1.12, rr = 0.16, edge = [];
      edge.push([[-w + rr, -h, 0], [w - rr, -h, 0]], [[-w + rr, h, 0], [w - rr, h, 0]], [[-w, -h + rr, 0], [-w, h - rr, 0]], [[w, -h + rr, 0], [w, h - rr, 0]]);
      const line = onSegments(edge);
      const corners = [[w - rr, h - rr, 0], [-w + rr, h - rr, 1.5708], [-w + rr, -h + rr, 3.1416], [w - rr, -h + rr, 4.7124]];
      return fill(n, () => {
        const u = Math.random();
        if (u < 0.55) return jit(line(), 0.012);
        if (u < 0.68) { const c = corners[(Math.random() * 4) | 0]; const p = arc(c[0], c[1], rr, c[2], c[2] + 1.5708); return jit(p, 0.012); }
        if (u < 0.74) { const p = arc(0, -h + 0.13, 0.05, 0, 6.2832); return jit(p, 0.01); }           // home button
        if (u < 0.78) return [rnd(0.16), h - 0.1 + rnd(0.012), 0];                                       // speaker slot
        return [rnd(w - 0.1), rnd(h - 0.22) + 0.02, rnd(0.03) * Math.random()];                          // glowing screen
      });
    },
    clock: (n) => {
      const hands = onSegments([[[0, 0, 0], [-0.38, 0.38, 0]], [[0, 0, 0], [0.52, 0.52, 0]], [[0, 0, 0], [0.55, 0.55, 0]]]);
      return fill(n, () => {
        const u = Math.random();
        if (u < 0.55) return jit(arc(0, 0, 1, 0, 6.2832), 0.015);
        if (u < 0.7) { const k = (Math.random() * 12) | 0, a = k * 0.5236, r = 0.8 + Math.random() * 0.12; return jit([Math.cos(a) * r, Math.sin(a) * r, 0], 0.012); }
        if (u < 0.93) return jit(hands(), 0.012);
        return jit([0, 0, 0], 0.05);
      });
    },
    nodes: (n) => {
      const C = [[0, 0, 0], [0.85, 0.45, 0.2], [-0.8, 0.55, -0.1], [0.55, -0.75, -0.2], [-0.6, -0.65, 0.25], [0.1, 0.95, -0.35], [-0.05, -0.15, 0.95]];
      const E = [[0, 1], [0, 2], [0, 3], [0, 4], [1, 5], [2, 5], [3, 4], [0, 6], [1, 3], [2, 4]];
      const line = onSegments(E.map(([a, b]) => [C[a], C[b]]));
      return fill(n, () => {
        if (Math.random() < 0.55) { const c = C[(Math.random() * C.length) | 0], s = c === C[0] ? 0.14 : 0.09; return [c[0] + gauss() * s, c[1] + gauss() * s, c[2] + gauss() * s]; }
        return jit(line(), 0.012);
      });
    },
    globe: (n) => fill(n, () => {
      const u = Math.random();
      let lat, lon;
      if (u < 0.5) { lat = (((Math.random() * 7) | 0) - 3) * 0.39; lon = Math.random() * 6.2832; }
      else if (u < 0.92) { lon = ((Math.random() * 8) | 0) * 0.3927; lat = (Math.random() - 0.5) * Math.PI; }
      else { const r = 1.0 + Math.random() * 0.02; const v = [gauss(), gauss(), gauss()], l = Math.hypot(...v); return v.map((x) => (x / l) * r * 0.25); }
      return jit([Math.cos(lat) * Math.cos(lon), Math.sin(lat), Math.cos(lat) * Math.sin(lon)], 0.008);
    }),
    // mirrored gestures (gestures.py): a heart from two hands
    heart: (n) => fill(n, () => {
      const t = Math.random() * 6.2832, u = Math.random();
      const hx = (tt) => 16 * Math.pow(Math.sin(tt), 3) / 17, hy = (tt) => (13 * Math.cos(tt) - 5 * Math.cos(2 * tt) - 2 * Math.cos(3 * tt) - Math.cos(4 * tt)) / 17 + 0.12;
      if (u < 0.62) return jit([hx(t), hy(t), rnd(0.05)], 0.02);                // the outline
      const k = Math.sqrt(Math.random()) * 0.92;                                   // a softer glowing fill
      return jit([hx(t) * k, hy(t) * k + 0.04 * (1 - k), rnd(0.12) * (1 - k)], 0.02);
    }),
    gear: (n) => fill(n, () => {
      const u = Math.random();
      if (u < 0.7) {
        const t = Math.random() * 6.2832, tooth = Math.cos(t * 9) > 0.15 ? 1 : 0;
        const r = 0.82 + tooth * 0.2 + (Math.random() < 0.2 ? Math.random() * 0.2 * tooth : 0);
        return jit([Math.cos(t) * r, Math.sin(t) * r, rnd(0.06)], 0.01);
      }
      return jit(arc(0, 0, 0.32, 0, 6.2832, rnd(0.05)), 0.012);
    }),
  };

  // ------------------------------------------------------------------ shaders
  const VERT = /* glsl */`
    attribute vec3 aFrom;
    attribute vec3 aTo;
    attribute vec4 aSeed;
    uniform float uTime, uFace, uMix, uLevel, uMic, uTurb, uFlow, uSpin, uSize, uScale, uBreath, uRipple, uSparkle, uFlash, uSwirl;
    uniform vec3 uC1, uC2, uC3;
    varying vec3 vColor;
    varying float vAlpha;

    vec3 flow(vec3 p, float t) {
      vec3 a = vec3(sin(p.y * 1.7 + t * 0.9 + aSeed.y * 6.28), sin(p.z * 1.9 + t * 0.7 + aSeed.z * 6.28), sin(p.x * 1.5 + t * 1.1 + aSeed.w * 6.28));
      vec3 b = vec3(sin(p.z * 3.1 - t * 1.3), sin(p.x * 2.9 + t * 1.2), sin(p.y * 3.3 - t * 0.8));
      return a * 0.65 + b * 0.35;
    }
    mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, -s, s, c); }

    void main() {
      // staggered morph: each particle sets off a little after the last
      float m = clamp(uMix * 1.5 - aSeed.x * 0.5, 0.0, 1.0);
      m = m * m * (3.0 - 2.0 * m);
      vec3 p = mix(aFrom, aTo, m);
      float travel = sin(m * 3.14159);                       // particles loosen up mid-flight
      float r = length(p) + 1e-4;
      vec3 dir = p / r;

      p *= 1.0 + sin(uTime * 1.3 + aSeed.x * 0.6) * 0.025 * uBreath;           // breathing
      p += flow(p * 1.3, uTime * uFlow) * (0.035 + 0.05 * uTurb + 0.22 * travel); // drift
      // swirl around the vertical axis, faster near the centre (thinking vortex)
      p.xz = rot(uSwirl * uTime * (0.6 + 0.8 / (0.4 + r))) * p.xz;
      // Jarvis's voice: push outward in travelling ripples
      float wave = sin(r * 11.0 - uTime * 7.0 + aSeed.y * 1.5) * 0.5 + 0.5;
      p += dir * uLevel * (0.08 + 0.2 * aSeed.y + uRipple * 0.2 * wave);
      // the user's voice: gather in and lean toward the viewer
      p *= 1.0 - uMic * 0.14;
      p.z += uMic * 0.25 * (0.5 + aSeed.z);
      // round shapes spin; rings and icons face you with a gentle sway so they stay readable
      p.xz = rot(mix(uSpin, sin(uTime * 0.6) * 0.35, uFace)) * p.xz;

      vec4 mv = modelViewMatrix * vec4(p, 1.0);
      gl_Position = projectionMatrix * mv;
      float size = uSize * (0.55 + aSeed.z * 0.9) * (1.0 + uLevel * 0.3 + travel * 0.3);
      gl_PointSize = size * uScale / -mv.z;

      float mixc = clamp(aSeed.w + 0.25 * sin(uTime * 0.7 + r * 3.0 + aSeed.x * 6.28), 0.0, 1.0);
      vec3 col = mix(uC1, uC2, mixc);
      float spark = step(1.0 - uSparkle, aSeed.y) * (0.5 + 0.5 * sin(uTime * 9.0 + aSeed.x * 40.0));
      col = mix(col, uC3, spark * 0.8);
      col = mix(col, vec3(1.0, 0.23, 0.2), uFlash);
      vColor = col;
      vAlpha = clamp(0.55 + 0.45 * (p.z + 1.0) * 0.5, 0.25, 1.0) * (0.75 + spark * 0.25);
    }`;
  const FRAG = /* glsl */`
    uniform float uOpacity;
    varying vec3 vColor;
    varying float vAlpha;
    void main() {
      float d = length(gl_PointCoord - 0.5);
      if (d > 0.5) discard;
      float a = pow(1.0 - d * 2.0, 1.6);
      gl_FragColor = vec4(vColor, a * vAlpha * uOpacity);
    }`;

  // ------------------------------------------------------------------ orb
  async function start(container, opts = {}) {
    let THREE;
    try {
      THREE = await import("./vendor/three.module.min.js");
    } catch (e) { console.warn("[orb] Three.js failed to load", e); return null; }
    const probe = document.createElement("canvas");
    if (!(probe.getContext("webgl2") || probe.getContext("webgl"))) return null;

    let renderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: false, alpha: true, powerPreference: "high-performance" });
    } catch (e) { console.warn("[orb] WebGL unavailable", e); return null; }
    renderer.setClearColor(0x000000, 0);
    const canvas = renderer.domElement;
    canvas.id = "orb3d";
    canvas.setAttribute("aria-hidden", "true");
    container.appendChild(canvas);

    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(34, 1, 0.1, 50);
    camera.position.set(0, 0, 5.6);

    const uniforms = {
      uTime: { value: 0 }, uFace: { value: 0 }, uMix: { value: 1 }, uLevel: { value: 0 }, uMic: { value: 0 }, uTurb: { value: 0.6 },
      uFlow: { value: 0.8 }, uSpin: { value: 0 }, uSize: { value: 8 }, uScale: { value: 300 }, uBreath: { value: 1 },
      uRipple: { value: 0 }, uSparkle: { value: 0.06 }, uFlash: { value: 0 }, uSwirl: { value: 0 }, uOpacity: { value: 1 },
      uC1: { value: new THREE.Color() }, uC2: { value: new THREE.Color() }, uC3: { value: new THREE.Color() },
    };
    const material = new THREE.ShaderMaterial({ vertexShader: VERT, fragmentShader: FRAG, uniforms, transparent: true, depthWrite: false });
    let geometry = null, points = null, count = 0, shapeCache = {};

    // live state
    const S = {
      state: "idle", shape: "sphere", quality: opts.quality || "auto", lowPower: false, theme: opts.theme || "light",
      pal: palette(opts.character),
      tts: 0, ttsTarget: 0, mic: 0, micTarget: 0,
      aiMood: null, aiAt: 0, userMood: null, userAt: 0,
      task: null, taskUntil: 0, flash: 0, morphStart: 0, morphDur: 1.4, spin: 0,
      face: 0, cur: { c1: new THREE.Color(), c2: new THREE.Color(), c3: new THREE.Color(), speed: 0.8, turb: 0.6, spark: 0.06, swirl: 0 },
      running: true, last: performance.now(), t: 0, frames: [], fpsChecked: false,
      // hand gestures (webcam): the orb leans toward your hand, pinch-and-move spins it, palm/fist pulse it
      hand: null, handAt: 0, grab: null, rotX: 0, rotY: 0, velX: 0, velY: 0, bloom: 0, squeeze: 0,
      lean: new THREE.Vector3(),
    };

    function build(n) {
      count = n; shapeCache = {};
      const from = new Float32Array(n * 3), seed = new Float32Array(n * 4);
      for (let i = 0; i < n * 4; i++) seed[i] = Math.random();
      if (points) { scene.remove(points); geometry.dispose(); }
      geometry = new THREE.BufferGeometry();
      const to = shape(S.shape).slice();
      from.set(to);
      geometry.setAttribute("position", new THREE.BufferAttribute(to.slice(), 3));   // for bounds only
      geometry.setAttribute("aFrom", new THREE.BufferAttribute(from, 3));
      geometry.setAttribute("aTo", new THREE.BufferAttribute(to, 3));
      geometry.setAttribute("aSeed", new THREE.BufferAttribute(seed, 4));
      geometry.boundingSphere = new THREE.Sphere(new THREE.Vector3(), 3);
      points = new THREE.Points(geometry, material);
      points.frustumCulled = false;
      scene.add(points);
      uniforms.uMix.value = 1;
    }
    function shape(name) {
      if (name.startsWith("mirror:") && !(S.mirror && S.mirror.hands.length)) name = "sphere";
      if (name.startsWith("mirror:")) {     // a one-off shape built from the hand points (not cached)
        const p = handShape(S.mirror.hands)(count);
        const out = new Float32Array(count * 3);
        const idx = Array.from({ length: count }, (_, i) => i).sort(() => Math.random() - 0.5);
        for (let i = 0; i < count; i++) { const k = idx[i] * 3; out[i * 3] = p[k]; out[i * 3 + 1] = p[k + 1]; out[i * 3 + 2] = p[k + 2]; }
        return out;
      }
      if (!shapeCache[name]) {
        // shuffle so every shape uses particles evenly (the morph then looks like a flow, not a slide)
        const p = SHAPES[name](count), idx = new Uint32Array(count);
        for (let i = 0; i < count; i++) idx[i] = i;
        for (let i = count - 1; i > 0; i--) { const j = (Math.random() * (i + 1)) | 0; const t = idx[i]; idx[i] = idx[j]; idx[j] = t; }
        const out = new Float32Array(count * 3);
        for (let i = 0; i < count; i++) { const k = idx[i] * 3; out[i * 3] = p[k]; out[i * 3 + 1] = p[k + 1]; out[i * 3 + 2] = p[k + 2]; }
        shapeCache[name] = out;
      }
      return shapeCache[name];
    }
    function morphTo(name, dur = 1.4) {
      if (name === S.shape || !geometry) return;
      // freeze where particles are now (same easing as the shader) and head for the new shape
      const from = geometry.attributes.aFrom.array, to = geometry.attributes.aTo.array, seed = geometry.attributes.aSeed.array;
      const mix = uniforms.uMix.value;
      for (let i = 0; i < count; i++) {
        let m = Math.min(1, Math.max(0, mix * 1.5 - seed[i * 4] * 0.5)); m = m * m * (3 - 2 * m);
        for (let k = 0; k < 3; k++) { const j = i * 3 + k; from[j] = from[j] + (to[j] - from[j]) * m; }
      }
      to.set(shape(name));
      geometry.attributes.aFrom.needsUpdate = true;
      geometry.attributes.aTo.needsUpdate = true;
      S.shape = name; S.morphStart = S.t; S.morphDur = dur;
      uniforms.uMix.value = 0;
    }

    function particleCount() { return S.lowPower ? 5000 : 14000; }
    function applyQuality() {
      const want = S.quality === "low" || (S.quality === "auto" && S.autoLow);
      S.lowPower = !!want;
      renderer.setPixelRatio(S.lowPower ? 1 : Math.min(window.devicePixelRatio || 1, 2));
      if (count !== particleCount()) build(particleCount());
      resize();
    }
    function resize() {
      const w = canvas.clientWidth || container.clientWidth, h = canvas.clientHeight || w;
      if (!w || !h) return;
      renderer.setSize(w, h, false);
      camera.aspect = w / h; camera.updateProjectionMatrix();
      uniforms.uScale.value = h * renderer.getPixelRatio() * 0.004;
    }
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);

    // auto quality: battery or a weak machine starts low; a slow first few seconds drops to low
    S.autoLow = (navigator.hardwareConcurrency || 8) <= 4;
    try {
      if (navigator.getBattery) {
        const b = await navigator.getBattery();
        const onBatt = () => { S.autoLow = !b.charging || S.slowGpu; if (S.quality === "auto") applyQuality(); };
        b.addEventListener("chargingchange", onBatt);
        S.autoLow = S.autoLow || !b.charging;
      }
    } catch (_) {}
    applyQuality();

    // ------------------------------------------------------------ mood -> look
    function targetLook() {
      const now = performance.now() / 1000;
      let key = "neutral";
      if (S.state === "listening" || S.state === "transcribing") key = "listening";
      if (S.userMood && S.userMood !== "neutral" && now - S.userAt < 300) key = "u_" + S.userMood;
      if (S.aiMood && (now - S.aiAt < 25 || S.state === "speaking")) key = S.aiMood;
      let look = S.pal[key] || S.pal.neutral;
      const busy = S.state === "thinking" || S.state === "acting";
      return { look, busy };
    }
    const tmpA = new THREE.Color(), tmpB = new THREE.Color(), tmpC = new THREE.Color(), violetA = new THREE.Color(), violetB = new THREE.Color();
    function updateLook(dt) {
      const { look, busy } = targetLook();
      tmpA.set(look[0]); tmpB.set(look[1]); tmpC.set(look[2]);
      let speed = look[3], turb = look[4], spark = look[5], swirl = 0;
      if (busy) {      // thinking pulls the colours toward violet and starts the swirl
        violetA.set(S.pal.thinking[0]); violetB.set(S.pal.thinking[1]);
        tmpA.lerp(violetA, 0.65); tmpB.lerp(violetB, 0.65);
        speed = Math.max(speed, 1.2); turb = Math.max(turb, 0.8); spark = Math.max(spark, 0.1);
        swirl = S.state === "thinking" ? 0.9 : 0;
      }
      if (S.theme === "light") {       // dense, deeper colours read better on a light background
        const hsl = {};
        for (const c of [tmpA, tmpB]) { c.getHSL(hsl); c.setHSL(hsl.h, Math.min(1, hsl.s * 1.1), hsl.l * 0.72); }
        tmpC.copy(tmpA).lerp(new THREE.Color(1, 1, 1), 0.35);
      }
      const k = 1 - Math.exp(-dt * 2.2);
      S.cur.c1.lerp(tmpA, k); S.cur.c2.lerp(tmpB, k); S.cur.c3.lerp(tmpC, k);
      S.cur.speed += (speed - S.cur.speed) * k; S.cur.turb += (turb - S.cur.turb) * k;
      S.cur.spark += (spark - S.cur.spark) * k; S.cur.swirl += (swirl - S.cur.swirl) * k;
    }
    function applyTheme() {
      const light = S.theme === "light";
      material.blending = light ? THREE.NormalBlending : THREE.AdditiveBlending;
      uniforms.uOpacity.value = light ? 0.9 : 0.85;
      material.needsUpdate = true;
    }
    applyTheme();

    // ------------------------------------------------------------ loop
    let raf = 0;
    function frame(now) {
      raf = requestAnimationFrame(frame);
      if (!S.running) return;
      const minDt = S.lowPower ? 1000 / 30 : 0;
      if (now - S.last < minDt - 1) return;
      const dt = Math.min(0.1, (now - S.last) / 1000);
      S.last = now;
      S.t += dt;
      // after a warm-up, measure real frame times once and drop to low power if the GPU struggles
      if (!S.fpsChecked && S.t > 1.5) {
        S.frames.push(dt);
        if (S.frames.length >= 90) {
          S.fpsChecked = true;
          const avg = S.frames.reduce((a, b) => a + b, 0) / S.frames.length;
          if (avg > 0.028 && !S.lowPower) { S.slowGpu = true; S.autoLow = true; if (S.quality === "auto") applyQuality(); }
        }
      }

      // the shape for right now: a task icon while a tool runs, else the state's shape
      const nowS = now / 1000;
      let want = STATE_SHAPE[S.state] || "sphere";
      if (S.task && (S.state === "acting" || nowS < S.taskUntil)) want = S.task;
      else if (S.task && nowS >= S.taskUntil) S.task = null;
      if (S.mirror) want = S.mirror.shape === "heart" ? "heart" : "mirror:" + S.mirror.id;   // copying your gesture
      morphTo(want);

      // audio: rise fast, fall slower
      S.tts += (S.ttsTarget - S.tts) * (S.ttsTarget > S.tts ? 0.35 : 0.08);
      S.mic += (S.micTarget - S.mic) * (S.micTarget > S.mic ? 0.3 : 0.07);
      S.ttsTarget *= 0.94; S.micTarget *= 0.93;
      S.flash *= Math.exp(-dt * 2.5);
      updateLook(dt);

      const speaking = S.state === "speaking";
      S.spin += dt * (0.12 + 0.18 * S.cur.speed + S.tts * 0.5);
      const faces = !(S.shape === "sphere" || S.shape === "vortex");
      if (S.mirror && S.mirror.shape === "heart") S.bloom = Math.max(S.bloom, 0.15 + 0.15 * Math.max(0, Math.sin(S.t * 5.5)));  // heartbeat
      S.face += ((faces ? 1 : 0) - S.face) * (1 - Math.exp(-dt * 3));
      uniforms.uFace.value = S.face;
      uniforms.uTime.value = S.t;
      uniforms.uMix.value = Math.min(1, (S.t - S.morphStart) / S.morphDur);
      uniforms.uLevel.value = Math.min(1.2, S.tts);
      uniforms.uMic.value = Math.min(1, S.mic);
      uniforms.uTurb.value = S.cur.turb + S.tts * 0.6;
      uniforms.uFlow.value = S.cur.speed;
      uniforms.uSpin.value = S.spin;
      uniforms.uBreath.value = S.state === "idle" ? 1 : 0.4;
      uniforms.uRipple.value = speaking ? 1 : 0.3;
      uniforms.uSparkle.value = S.cur.spark + S.tts * 0.08;
      uniforms.uSwirl.value = S.cur.swirl;
      uniforms.uFlash.value = Math.min(0.85, S.flash * (0.6 + 0.4 * Math.sin(S.t * 30)));
      uniforms.uSize.value = S.lowPower ? 10 : 7;
      uniforms.uC1.value.copy(S.cur.c1); uniforms.uC2.value.copy(S.cur.c2); uniforms.uC3.value.copy(S.cur.c3);
      handMotion(dt, now);
      renderer.render(scene, camera);
    }
    // ------------------------------------------------------------ hand gestures
    const leanTo = new THREE.Vector3();
    function handMotion(dt, now) {
      const h = S.hand && now - S.handAt < 500 ? S.hand : null;
      if (h && h.pinch) {                       // grab and spin: hand movement becomes spin velocity
        if (S.grab) { S.velY += (h.x - S.grab.x) * 55; S.velX += (h.y - S.grab.y) * 40; }
        S.grab = { x: h.x, y: h.y };
      } else S.grab = null;
      S.rotY += S.velY * dt; S.rotX += S.velX * dt;
      const damp = Math.exp(-dt * (S.grab ? 6 : 1.6));
      S.velX *= damp; S.velY *= damp;
      if (!S.grab) S.rotX *= Math.exp(-dt * 0.8);   // tilt settles back, spin keeps its angle
      S.bloom *= Math.exp(-dt * 2.2); S.squeeze *= Math.exp(-dt * 2.8);
      if (h) leanTo.set((h.x - 0.5) * 0.9, -(h.y - 0.5) * 0.6, 0); else leanTo.set(0, 0, 0);
      S.lean.lerp(leanTo, 1 - Math.exp(-dt * 3));
      if (!points) return;
      points.rotation.set(S.rotX, S.rotY, 0);
      points.position.copy(S.lean);
      points.scale.setScalar(1 + 0.28 * Math.sin(Math.min(1, S.bloom) * Math.PI / 2) - 0.3 * S.squeeze);
    }

    const onVis = () => { S.running = !document.hidden; S.last = performance.now(); };
    document.addEventListener("visibilitychange", onVis);
    // start with the right colours rather than fading in from black
    updateLook(10);
    raf = requestAnimationFrame(frame);

    // ------------------------------------------------------------ controller
    return {
      setState(st) { S.state = st; if (st !== "acting" && S.task && performance.now() / 1000 >= S.taskUntil) S.task = null; },
      level(source, v) { if (source === "tts") S.ttsTarget = Math.max(S.ttsTarget, v); else S.micTarget = Math.max(S.micTarget, v); },
      userMood(m) { S.userMood = m || null; S.userAt = performance.now() / 1000; },
      aiMood(m) { if (MOODS[m]) { S.aiMood = m; S.aiAt = performance.now() / 1000; } },
      task(tool, seconds = 2.4) { S.task = taskShape(tool); S.taskUntil = performance.now() / 1000 + seconds; },
      flash() { S.flash = 1; },
      hand(h) { S.hand = h || null; S.handAt = performance.now(); },
      gesture(g) {
        if (g === "palm") S.bloom = 1;
        else if (g === "fist") { S.squeeze = 1; S.velX = S.velY = 0; }
        else if (g === "swipe_left") S.velY -= 5;
        else if (g === "swipe_right") S.velY += 5;
        else if (g === "swipe_up") S.velX -= 4;
        else if (g === "swipe_down") S.velX += 4;
        else if (g === "pinch_tap") S.bloom = 0.5;
      },
      // copy a gesture the user is holding up (gestures.py MirrorTracker): {shape, hands} or {shape: null}
      mirror(ev) {
        if (!ev || !ev.shape) { S.mirror = null; return; }
        S.mirror = { shape: ev.shape, hands: ev.hands || [], id: (S.mirrorN = (S.mirrorN || 0) + 1) };
        if (ev.shape !== "heart" && !S.mirror.hands.length) S.mirror = null;
      },
      setCharacter(c) { S.pal = palette(c); },     // colours glide over to the new palette
      setTheme(t) { S.theme = t === "dark" ? "dark" : "light"; applyTheme(); },
      setQuality(q) { S.quality = q || "auto"; applyQuality(); },
      get info() { return { shape: S.shape, state: S.state, particles: count, lowPower: S.lowPower, mood: targetLook() }; },
      destroy() {
        cancelAnimationFrame(raf); ro.disconnect(); document.removeEventListener("visibilitychange", onVis);
        geometry && geometry.dispose(); material.dispose(); renderer.dispose(); canvas.remove();
      },
    };
  }

  window.JarvisOrb = { start, taskShape, moods: Object.keys(MOODS), palettes: PALETTES };
})();
