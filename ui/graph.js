// Live knowledge-graph view: a small force-directed layout on <canvas>, no dependencies.
// Usage: const g = new MemoryGraph(canvas, {onSelect}); g.setData({nodes, edges}); g.highlight(ids, edgeIds)
(function () {
  const TYPE_COLORS = {
    Person: "#7af0ff", Organization: "#ffb547", Place: "#4be3a0", Project: "#b18cff", Preference: "#ff7eb6",
    Event: "#ffd86b", Task: "#ff9361", Device: "#8fb7ff", Skill: "#6be0c8", Concept: "#c9d3df", Thing: "#9fb0c3",
  };
  const colorOf = (t) => TYPE_COLORS[t] || TYPE_COLORS.Thing;

  class MemoryGraph {
    constructor(canvas, opts = {}) {
      this.cv = canvas;
      this.cx = canvas.getContext("2d");
      this.opts = opts;
      this.nodes = new Map();   // id -> node
      this.edges = [];
      this.hl = new Map();      // id -> {until, kind}
      this.view = { x: 0, y: 0, k: 1 };
      this.hover = null;
      this.selected = null;
      this.alpha = 1;
      this.visible = false;
      this._bind();
      requestAnimationFrame((t) => this._frame(t));
    }

    setData({ nodes, edges }) {
      const old = this.nodes;
      this.nodes = new Map();
      const n = nodes.length || 1;
      nodes.forEach((d, i) => {
        const prev = old.get(d.id);
        const a = (i / n) * Math.PI * 2;
        this.nodes.set(d.id, Object.assign(prev || {
          x: Math.cos(a) * 160 + (Math.random() - 0.5) * 20,
          y: Math.sin(a) * 160 + (Math.random() - 0.5) * 20, vx: 0, vy: 0,
        }, d));
      });
      if (this.nodes.has("user")) {
        const u = this.nodes.get("user");
        if (!old.has("user")) { u.x = 0; u.y = 0; }
      }
      this.edges = edges.filter((e) => this.nodes.has(e.source) && this.nodes.has(e.target));
      this.degree = new Map();
      for (const e of this.edges) {
        this.degree.set(e.source, (this.degree.get(e.source) || 0) + 1);
        this.degree.set(e.target, (this.degree.get(e.target) || 0) + 1);
      }
      this.alpha = Math.max(this.alpha, 0.8);
    }

    // kind: "search" (cyan pulse) or "new" (green flash)
    highlight(nodeIds = [], edgeIds = [], kind = "search", ms = 7000) {
      const until = performance.now() + ms;
      for (const id of nodeIds) this.hl.set(id, { until, kind });
      for (const id of edgeIds) this.hl.set("e:" + id, { until, kind });
      this.alpha = Math.max(this.alpha, 0.3);
    }

    fit() {
      if (!this.nodes.size) return;
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      for (const n of this.nodes.values()) { minX = Math.min(minX, n.x); minY = Math.min(minY, n.y); maxX = Math.max(maxX, n.x); maxY = Math.max(maxY, n.y); }
      const W = this.cv.clientWidth, H = this.cv.clientHeight;
      if (!W || !H) return;
      const k = Math.min(2, 0.85 * Math.min(W / (maxX - minX + 80), H / (maxY - minY + 80)));
      this.view = { k, x: -(minX + maxX) / 2, y: -(minY + maxY) / 2 };
    }

    // ---------------------------------------------------------------- physics
    _tick() {
      const ns = [...this.nodes.values()];
      const a = this.alpha;
      if (a < 0.005) return;
      for (let i = 0; i < ns.length; i++) {
        const p = ns[i];
        for (let j = i + 1; j < ns.length; j++) {
          const q = ns[j];
          let dx = p.x - q.x, dy = p.y - q.y;
          let d2 = dx * dx + dy * dy + 0.01;
          if (d2 > 250000) continue;
          const f = (1600 / d2) * a;
          const d = Math.sqrt(d2);
          dx /= d; dy /= d;
          p.vx += dx * f * 4; p.vy += dy * f * 4; q.vx -= dx * f * 4; q.vy -= dy * f * 4;
        }
      }
      for (const e of this.edges) {
        const s = this.nodes.get(e.source), t = this.nodes.get(e.target);
        const dx = t.x - s.x, dy = t.y - s.y;
        const d = Math.sqrt(dx * dx + dy * dy) + 0.01;
        const f = (d - 90) * 0.02 * a;
        s.vx += (dx / d) * f; s.vy += (dy / d) * f; t.vx -= (dx / d) * f; t.vy -= (dy / d) * f;
      }
      for (const n of ns) {
        if (n === this.drag) continue;
        n.vx -= n.x * 0.004 * a; n.vy -= n.y * 0.004 * a;
        if (n.id === "user") { n.vx -= n.x * 0.05; n.vy -= n.y * 0.05; }
        n.vx *= 0.82; n.vy *= 0.82;
        n.x += n.vx; n.y += n.vy;
      }
      this.alpha *= 0.992;
    }

    // ---------------------------------------------------------------- render
    _toScreen(n) {
      const W = this.cv.width, H = this.cv.height, dpr = this.dpr;
      return [W / 2 + (n.x + this.view.x) * this.view.k * dpr, H / 2 + (n.y + this.view.y) * this.view.k * dpr];
    }
    _radius(n) { return (n.id === "user" ? 11 : 5 + Math.min(6, (this.degree.get(n.id) || 0))) ; }

    _frame(t) {
      requestAnimationFrame((tt) => this._frame(tt));
      if (!this.visible) return;
      this._resize();
      this._tick();
      const cx = this.cx, dpr = this.dpr, k = this.view.k, now = performance.now();
      cx.clearRect(0, 0, this.cv.width, this.cv.height);
      for (const [id, h] of this.hl) if (h.until < now) this.hl.delete(id);
      const anyHl = [...this.hl.values()].some((h) => h.kind === "search");
      const pulse = 0.5 + 0.5 * Math.sin(now / 180);

      // edges
      for (const e of this.edges) {
        const s = this.nodes.get(e.source), tg = this.nodes.get(e.target);
        const [x1, y1] = this._toScreen(s), [x2, y2] = this._toScreen(tg);
        const h = this.hl.get("e:" + e.id);
        const sel = this.selected && (e.source === this.selected || e.target === this.selected);
        cx.beginPath(); cx.moveTo(x1, y1); cx.lineTo(x2, y2);
        if (h) {
          cx.strokeStyle = h.kind === "new" ? `rgba(75,227,160,${0.5 + pulse * 0.5})` : `rgba(122,240,255,${0.5 + pulse * 0.5})`;
          cx.lineWidth = 2.5 * dpr;
        } else {
          cx.strokeStyle = sel ? "rgba(216,230,243,0.6)" : (anyHl ? "rgba(60,90,120,0.18)" : "rgba(80,120,160,0.35)");
          cx.lineWidth = (sel ? 1.6 : 1) * dpr;
        }
        cx.stroke();
        if ((h || sel || this.hover === e.source || this.hover === e.target) && k > 0.5) {
          cx.fillStyle = h ? "#d8f6ff" : "rgba(170,190,210,0.85)";
          cx.font = `${10 * dpr}px Segoe UI, system-ui, sans-serif`;
          cx.textAlign = "center";
          cx.fillText(e.relation.replace(/_/g, " "), (x1 + x2) / 2, (y1 + y2) / 2 - 3 * dpr);
        }
      }
      // nodes
      for (const n of this.nodes.values()) {
        const [x, y] = this._toScreen(n);
        const r = this._radius(n) * dpr * Math.max(0.6, Math.min(1.4, k));
        const h = this.hl.get(n.id);
        const col = colorOf(n.type);
        if (h) {
          const g = cx.createRadialGradient(x, y, r, x, y, r * (3 + pulse * 2));
          const c = h.kind === "new" ? "75,227,160" : "122,240,255";
          g.addColorStop(0, `rgba(${c},0.55)`); g.addColorStop(1, `rgba(${c},0)`);
          cx.fillStyle = g; cx.beginPath(); cx.arc(x, y, r * (3 + pulse * 2), 0, Math.PI * 2); cx.fill();
        }
        cx.globalAlpha = anyHl && !h && n.id !== this.selected ? 0.35 : 1;
        cx.beginPath(); cx.arc(x, y, r, 0, Math.PI * 2);
        cx.fillStyle = col; cx.fill();
        if (n.id === this.selected || n.id === this.hover) { cx.lineWidth = 2 * dpr; cx.strokeStyle = "#fff"; cx.stroke(); }
        if (k > 0.45 || h || n.id === "user") {
          cx.fillStyle = h ? "#ffffff" : "rgba(216,230,243,0.9)";
          cx.font = `${(n.id === "user" ? 13 : 11) * dpr}px Segoe UI, system-ui, sans-serif`;
          cx.textAlign = "center";
          cx.fillText(n.name, x, y + r + 13 * dpr);
        }
        cx.globalAlpha = 1;
      }
      if (!this.nodes.size) {
        cx.fillStyle = "rgba(125,143,163,0.9)"; cx.font = `${13 * dpr}px Segoe UI, system-ui, sans-serif`; cx.textAlign = "center";
        cx.fillText("No memories yet. Tell me about yourself.", this.cv.width / 2, this.cv.height / 2);
      }
    }

    _resize() {
      const dpr = window.devicePixelRatio || 1;
      const w = Math.round(this.cv.clientWidth * dpr), h = Math.round(this.cv.clientHeight * dpr);
      if (this.cv.width !== w || this.cv.height !== h) { this.cv.width = w; this.cv.height = h; }
      this.dpr = dpr;
    }

    // ---------------------------------------------------------------- interaction
    _pick(ev) {
      const rect = this.cv.getBoundingClientRect();
      const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
      const W = rect.width, H = rect.height, k = this.view.k;
      const gx = (mx - W / 2) / k - this.view.x, gy = (my - H / 2) / k - this.view.y;
      let best = null, bd = 18 / k;
      for (const n of this.nodes.values()) {
        const d = Math.hypot(n.x - gx, n.y - gy);
        if (d < bd) { bd = d; best = n; }
      }
      return { node: best, gx, gy };
    }

    _bind() {
      const cv = this.cv;
      let panning = null;
      cv.addEventListener("pointerdown", (ev) => {
        const { node } = this._pick(ev);
        cv.setPointerCapture(ev.pointerId);
        if (node) { this.drag = node; this.moved = false; }
        else panning = { x: ev.clientX, y: ev.clientY, vx: this.view.x, vy: this.view.y };
      });
      cv.addEventListener("pointermove", (ev) => {
        if (this.drag) {
          const { gx, gy } = this._pick(ev);
          this.drag.x = gx; this.drag.y = gy; this.drag.vx = this.drag.vy = 0; this.moved = true; this.alpha = Math.max(this.alpha, 0.2);
        } else if (panning) {
          this.view.x = panning.vx + (ev.clientX - panning.x) / this.view.k;
          this.view.y = panning.vy + (ev.clientY - panning.y) / this.view.k;
        } else {
          const { node } = this._pick(ev);
          this.hover = node ? node.id : null;
          cv.style.cursor = node ? "pointer" : "grab";
          cv.title = node ? `${node.name} (${node.type})${node.summary ? "\n" + node.summary : ""}` : "";
        }
      });
      cv.addEventListener("pointerup", () => {
        if (this.drag && !this.moved) { this.selected = this.drag.id; this.opts.onSelect && this.opts.onSelect(this.drag); }
        else if (!this.drag && panning && this.selected) { /* keep */ }
        this.drag = null; panning = null;
      });
      cv.addEventListener("wheel", (ev) => {
        ev.preventDefault();
        this.view.k = Math.max(0.2, Math.min(4, this.view.k * (ev.deltaY < 0 ? 1.12 : 0.89)));
      }, { passive: false });
      cv.addEventListener("dblclick", () => this.fit());
    }
  }
  window.MemoryGraph = MemoryGraph;
})();
