//! Tauri shell for Jarvis. It opens the window and launches the Python agent backend
//! (`python -m jarvis.server` on 127.0.0.1:8765), and stops it again on exit.
//! It also owns the screen HUD: one transparent, click-through, always-on-top window per monitor
//! (ui/hud.html), excluded from screen capture so Jarvis's own screenshots never include it.

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, PhysicalPosition, PhysicalSize, RunEvent, WebviewUrl, WebviewWindowBuilder, WindowEvent};

struct Backend(Mutex<Option<Child>>);

const PORT: u16 = 8765;

fn backend_running() -> bool {
    TcpStream::connect_timeout(&([127, 0, 0, 1], PORT).into(), Duration::from_millis(300)).is_ok()
}

/// Where the `jarvis` Python package lives: $JARVIS_BACKEND_DIR, the bundled resources,
/// or `backend/` next to this source tree (dev mode).
fn backend_dir(app: &tauri::AppHandle) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(dir) = std::env::var("JARVIS_BACKEND_DIR") {
        candidates.push(PathBuf::from(dir));
    }
    candidates.push(Path::new(env!("CARGO_MANIFEST_DIR")).join("../../backend"));
    if let Ok(res) = app.path().resource_dir() {
        candidates.push(res.join("backend"));
    }
    candidates.into_iter().find(|d| d.join("jarvis").join("server.py").exists())
}

/// Python interpreter: $JARVIS_PYTHON, the backend's .venv, or python on PATH.
fn python_for(dir: &Path) -> PathBuf {
    if let Ok(p) = std::env::var("JARVIS_PYTHON") {
        return PathBuf::from(p);
    }
    let venv = if cfg!(windows) {
        dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        dir.join(".venv").join("bin").join("python")
    };
    if venv.exists() {
        return venv;
    }
    PathBuf::from(if cfg!(windows) { "python" } else { "python3" })
}

/// The self-contained backend that the release installer ships (PyInstaller build), if present.
fn bundled_backend(app: &tauri::AppHandle) -> Option<PathBuf> {
    if std::env::var("JARVIS_PYTHON").is_ok() || std::env::var("JARVIS_BACKEND_DIR").is_ok() {
        return None; // explicit dev override
    }
    let exe = if cfg!(windows) { "jarvis-backend.exe" } else { "jarvis-backend" };
    let res = app.path().resource_dir().ok()?;
    let p = res.join("backend-bin").join(exe);
    p.exists().then_some(p)
}

fn hide_window(cmd: &mut Command) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    let _ = cmd;
}

fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    if backend_running() {
        println!("[jarvis] backend already running on port {PORT}");
        return None;
    }
    if let Some(exe) = bundled_backend(app) {
        println!("[jarvis] starting bundled backend: {}", exe.display());
        let mut cmd = Command::new(&exe);
        cmd.env("JARVIS_PORT", PORT.to_string()).stdin(Stdio::null());
        if let Some(dir) = exe.parent() {
            cmd.current_dir(dir);
        }
        hide_window(&mut cmd);
        match cmd.spawn() {
            Ok(child) => return Some(child),
            Err(e) => eprintln!("[jarvis] failed to start bundled backend: {e}"),
        }
    }
    let Some(dir) = backend_dir(app) else {
        eprintln!("[jarvis] could not find the backend folder; set JARVIS_BACKEND_DIR");
        return None;
    };
    let python = python_for(&dir);
    println!("[jarvis] starting backend: {} -m jarvis.server (in {})", python.display(), dir.display());

    let mut cmd = Command::new(&python);
    cmd.args(["-m", "jarvis.server"])
        .current_dir(&dir)
        .env("PYTHONUNBUFFERED", "1")
        .env("JARVIS_PORT", PORT.to_string())
        .stdin(Stdio::null());
    hide_window(&mut cmd);
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("[jarvis] failed to start backend with {}: {e}", python.display());
            None
        }
    }
}

// ------------------------------------------------------------------ screen HUD

fn close_huds(app: &tauri::AppHandle) {
    for (label, w) in app.webview_windows() {
        if label.starts_with("hud-") {
            let _ = w.destroy();
        }
    }
}

/// Show (one overlay per monitor) or remove the screen HUD. Async so window creation can't deadlock on Windows.
#[tauri::command]
async fn hud_sync(app: tauri::AppHandle, enabled: bool) -> Result<usize, String> {
    close_huds(&app);
    if !enabled {
        return Ok(0);
    }
    let monitors = app.available_monitors().map_err(|e| e.to_string())?;
    let primary = app.primary_monitor().ok().flatten().map(|m| *m.position());
    let mut made = 0;
    for (i, m) in monitors.iter().enumerate() {
        let pos = *m.position();
        let size = *m.size();
        let scale = m.scale_factor();
        let info = format!(
            "window.__HUD__ = {{ index: {i}, x: {}, y: {}, w: {}, h: {}, scale: {scale}, primary: {} }};",
            pos.x,
            pos.y,
            size.width,
            size.height,
            primary.map(|p| p == pos).unwrap_or(i == 0)
        );
        // One pixel short of the full height: Windows treats a topmost window that covers the whole
        // monitor as a full-screen app and would hide the taskbar.
        let h = size.height.saturating_sub(1).max(1);
        let built = WebviewWindowBuilder::new(&app, format!("hud-{i}"), WebviewUrl::App("hud.html".into()))
            .title("Jarvis HUD")
            .initialization_script(&info)
            .transparent(true)
            .decorations(false)
            .shadow(false)
            .resizable(false)
            .always_on_top(true)
            .skip_taskbar(true)
            .focused(false)
            .visible(false)
            .content_protected(true)
            .visible_on_all_workspaces(true)
            .inner_size(size.width as f64 / scale, h as f64 / scale)
            .position(pos.x as f64 / scale, pos.y as f64 / scale)
            .build();
        match built {
            Ok(w) => {
                let _ = w.set_position(PhysicalPosition::new(pos.x, pos.y));
                let _ = w.set_size(PhysicalSize::new(size.width, h));
                let _ = w.set_ignore_cursor_events(true);
                let _ = w.set_content_protected(true);
                let _ = w.show();
                made += 1;
            }
            Err(e) => eprintln!("[jarvis] couldn't open the HUD on monitor {i}: {e}"),
        }
    }
    Ok(made)
}

/// Quick pinch gesture: bring Jarvis to the front, or tuck it away if it already is.
#[tauri::command]
async fn toggle_main(app: tauri::AppHandle) -> Result<bool, String> {
    let w = app.get_webview_window("main").ok_or("no main window")?;
    let front = w.is_visible().unwrap_or(false) && !w.is_minimized().unwrap_or(false) && w.is_focused().unwrap_or(false);
    if front {
        w.minimize().map_err(|e| e.to_string())?;
    } else {
        let _ = w.unminimize();
        let _ = w.show();
        w.set_focus().map_err(|e| e.to_string())?;
    }
    Ok(!front)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let child = spawn_backend(app.handle());
            app.manage(Backend(Mutex::new(child)));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![hud_sync, toggle_main])
        .on_window_event(|window, event| {
            // The HUD windows are helpers: closing the main window quits the app.
            if window.label() == "main" {
                if let WindowEvent::Destroyed = event {
                    close_huds(window.app_handle());
                    window.app_handle().exit(0);
                }
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building Jarvis");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(state) = handle.try_state::<Backend>() {
                if let Some(mut child) = state.0.lock().unwrap().take() {
                    let _ = child.kill();
                    let _ = child.wait();
                }
            }
        }
    });
}
