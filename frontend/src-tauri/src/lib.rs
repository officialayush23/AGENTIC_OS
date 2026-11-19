//! Agentic OS desktop shell.
//!
//! On start-up the shell:
//!   1. picks a free loopback port and generates a random 256-bit bearer token,
//!   2. starts the Python backend (dev: `python -m agentic_os` from ../../backend;
//!      release: the bundled `agentic-os-backend` sidecar built with PyInstaller),
//!   3. writes the token to the backend's stdin (never on the command line, where other
//!      processes could read it), and waits for its `AGENTIC_OS_READY` line,
//!   4. hands `{url, token}` to the UI through the `backend_info` command.
//! When the app exits, the backend is killed.

use std::net::TcpListener;
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use serde::Serialize;
use tauri::{Manager, RunEvent};
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;

#[derive(Clone, Serialize)]
struct BackendInfo {
    url: String,
    token: String,
}

#[derive(Default)]
struct Backend {
    info: Mutex<Option<BackendInfo>>,
    error: Mutex<Option<String>>,
    child: Mutex<Option<CommandChild>>,
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .and_then(|l| l.local_addr())
        .map(|a| a.port())
        .unwrap_or(8765)
}

fn random_token() -> String {
    let bytes: [u8; 32] = rand::random();
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// Folder holding the Python package when running from a source checkout.
fn dev_backend_dir() -> PathBuf {
    if let Ok(dir) = std::env::var("AGENTIC_OS_BACKEND_DIR") {
        return PathBuf::from(dir);
    }
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("..").join("backend")
}

fn dev_python(backend_dir: &PathBuf) -> String {
    if let Ok(py) = std::env::var("AGENTIC_OS_PYTHON") {
        return py;
    }
    let venv = if cfg!(windows) {
        backend_dir.join(".venv").join("Scripts").join("python.exe")
    } else {
        backend_dir.join(".venv").join("bin").join("python")
    };
    if venv.exists() {
        venv.to_string_lossy().into_owned()
    } else if cfg!(windows) {
        "python".into()
    } else {
        "python3".into()
    }
}

fn start_backend(app: &tauri::AppHandle) -> Result<(), String> {
    let port = free_port();
    let token = random_token();
    let port_arg = port.to_string();

    let command = if cfg!(debug_assertions) {
        let dir = dev_backend_dir();
        app.shell()
            .command(dev_python(&dir))
            .args(["-m", "agentic_os", "--port", &port_arg, "--token-stdin", "--no-browser", "--dev"])
            .current_dir(dir)
            .env("PYTHONUNBUFFERED", "1")
    } else {
        app.shell()
            .sidecar("agentic-os-backend")
            .map_err(|e| format!("backend sidecar missing: {e}"))?
            .args(["--port", &port_arg, "--token-stdin", "--no-browser"])
    };

    let (mut rx, mut child) = command.spawn().map_err(|e| format!("could not start the backend: {e}"))?;
    child
        .write(format!("{token}\n").as_bytes())
        .map_err(|e| format!("could not hand the token to the backend: {e}"))?;

    let state = app.state::<Backend>();
    *state.child.lock().unwrap() = Some(child);

    let handle = app.clone();
    let url = format!("http://127.0.0.1:{port}");
    tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            let state = handle.state::<Backend>();
            match event {
                CommandEvent::Stdout(line) => {
                    let line = String::from_utf8_lossy(&line);
                    if line.starts_with("AGENTIC_OS_READY") {
                        *state.info.lock().unwrap() = Some(BackendInfo { url: url.clone(), token: token.clone() });
                    }
                }
                CommandEvent::Stderr(line) => {
                    eprint!("[backend] {}", String::from_utf8_lossy(&line));
                }
                CommandEvent::Terminated(payload) => {
                    let msg = format!("the backend stopped (exit code {:?})", payload.code);
                    eprintln!("[shell] {msg}");
                    *state.info.lock().unwrap() = None;
                    *state.error.lock().unwrap() = Some(msg);
                }
                CommandEvent::Error(err) => eprintln!("[shell] backend error: {err}"),
                _ => {}
            }
        }
    });
    Ok(())
}

/// Resolves once the backend is listening (or fails after 60 s).
#[tauri::command]
async fn backend_info(state: tauri::State<'_, Backend>) -> Result<BackendInfo, String> {
    let deadline = Instant::now() + Duration::from_secs(60);
    loop {
        if let Some(info) = state.info.lock().unwrap().clone() {
            return Ok(info);
        }
        if let Some(err) = state.error.lock().unwrap().clone() {
            return Err(err);
        }
        if Instant::now() > deadline {
            return Err("the backend did not start within 60 seconds".into());
        }
        tokio_sleep(Duration::from_millis(100)).await;
    }
}

async fn tokio_sleep(d: Duration) {
    let _ = tauri::async_runtime::spawn_blocking(move || std::thread::sleep(d)).await;
}

/// Native folder picker for choosing the workspace.
#[tauri::command]
async fn pick_folder(app: tauri::AppHandle) -> Result<Option<String>, String> {
    use tauri_plugin_dialog::DialogExt;
    let picked = tauri::async_runtime::spawn_blocking(move || app.dialog().file().blocking_pick_folder())
        .await
        .map_err(|e| e.to_string())?;
    Ok(picked.and_then(|p| p.into_path().ok()).map(|p| p.to_string_lossy().into_owned()))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Backend::default())
        .invoke_handler(tauri::generate_handler![backend_info, pick_folder])
        .setup(|app| {
            if let Err(err) = start_backend(app.handle()) {
                eprintln!("[shell] {err}");
                *app.state::<Backend>().error.lock().unwrap() = Some(err);
            }
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building the Agentic OS shell");

    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            if let Some(child) = handle.state::<Backend>().child.lock().unwrap().take() {
                let _ = child.kill();
            }
        }
    });
}
