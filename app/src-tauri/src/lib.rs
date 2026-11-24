//! Tauri shell for Jarvis. It opens the window and launches the Python agent backend
//! (`python -m jarvis.server` on 127.0.0.1:8765), and stops it again on exit.

use std::net::TcpStream;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;

use tauri::{Manager, RunEvent};

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

fn spawn_backend(app: &tauri::AppHandle) -> Option<Child> {
    if backend_running() {
        println!("[jarvis] backend already running on port {PORT}");
        return None;
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
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    match cmd.spawn() {
        Ok(child) => Some(child),
        Err(e) => {
            eprintln!("[jarvis] failed to start backend with {}: {e}", python.display());
            None
        }
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .setup(|app| {
            let child = spawn_backend(app.handle());
            app.manage(Backend(Mutex::new(child)));
            Ok(())
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
