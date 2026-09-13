/* Mi-Ripple Studio — Electron shell.
 *
 * Launches the bundled offline Gradio backend (a frozen Python server) on a
 * random localhost port, waits for its "MI_RIPPLE_READY <port>" signal, then
 * loads it in a full-window BrowserWindow. All external network access is
 * blocked; the app is fully offline by design.
 */
"use strict";

const { app, BrowserWindow, Menu, dialog, session } = require("electron");
const { spawn, execFile } = require("node:child_process");
const net = require("node:net");
const path = require("node:path");

// Hard offline guarantee: break DNS resolution for every host; only
// loopback (used by the bundled backend) is excluded. Must run before
// the app is ready.
app.commandLine.appendSwitch("host-resolver-rules", "MAP * ~NOTFOUND, EXCLUDE 127.0.0.1");

const APP_TITLE = "Mi-Ripple Studio";
const READY_TIMEOUT_MS = 180000; // python server cold start can be slow on first run
const LOG_DIR = () => path.join(app.getPath("userData"), "logs");

let serverProc = null;
let mainWindow = null;
let appPort = 0;
let serverLog = "";
let readyTimer = null;
let quitting = false;

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(" ")}`;
  console.log(line);
  try {
    const fs = require("node:fs");
    fs.mkdirSync(LOG_DIR(), { recursive: true });
    fs.appendFileSync(path.join(LOG_DIR(), "studio.log"), line + "\n");
  } catch (_) {}
}

// ---------------------------------------------------------------------------
// Backend resolution
// ---------------------------------------------------------------------------

/** Random free TCP port on loopback. */
function pickFreePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.unref();
    srv.on("error", reject);
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
  });
}

function resolveServer() {
  const fs = require("node:fs");
  if (process.platform === "win32") {
    // Packaged: resources\server\MiRippleServer\MiRippleServer.exe
    const exe = path.join(process.resourcesPath, "server", "MiRippleServer", "MiRippleServer.exe");
    if (fs.existsSync(exe)) {
      return { cmd: exe, args: [], cwd: path.dirname(exe), packaged: true };
    }
  } else {
    const exe = path.join(process.resourcesPath, "server", "MiRippleServer", "MiRippleServer");
    if (fs.existsSync(exe)) {
      return { cmd: exe, args: [], cwd: path.dirname(exe), packaged: true };
    }
  }
  // Development fallback: run from a venv next to the source tree.
  const python =
    process.env.MR_STUDIO_PYTHON ||
    (process.platform === "win32" ? "python" : "python3");
  const appDir = path.join(__dirname, "..", "app");
  const venvPy =
    process.platform === "win32"
      ? path.join(appDir, "..", ".venv", "Scripts", "python.exe")
      : path.join(appDir, "..", ".venv", "bin", "python");
  const py = fs.existsSync(venvPy) ? venvPy : python;
  return {
    cmd: py,
    args: [path.join(appDir, "gradio_app.py")],
    cwd: appDir,
    packaged: false,
  };
}

let serverInfo = null;

function serverArgs(port) {
  const examplesDir = serverInfo && serverInfo.packaged
    ? path.join(process.resourcesPath, "examples")
    : path.join(__dirname, "..", "examples");
  return [
    "--host",
    "127.0.0.1",
    "--port",
    String(port),
    "--data-dir",
    app.getPath("userData"),
    "--examples-dir",
    examplesDir,
  ];
}

// ---------------------------------------------------------------------------
// Backend lifecycle
// ---------------------------------------------------------------------------

function startServer(port) {
  const resolved = resolveServer();
  const { cmd, args, cwd } = resolved;
  serverInfo = resolved;
  log("starting backend:", cmd, ...args);
  serverProc = spawn(cmd, [...args, ...serverArgs(port)], {
    cwd,
    env: {
      ...process.env,
      GRADIO_ANALYTICS_ENABLED: "False",
      PYTHONUNBUFFERED: "1",
      // Force ASCII-safe logging for the frozen Windows build.
      PYTHONIOENCODING: "utf-8",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });

  serverProc.stdout.setEncoding("utf8");
  serverProc.stderr.setEncoding("utf8");
  let stdoutBuf = "";
  serverProc.stdout.on("data", (chunk) => {
    stdoutBuf += chunk;
    let idx;
    while ((idx = stdoutBuf.indexOf("\n")) >= 0) {
      const line = stdoutBuf.slice(0, idx).trim();
      stdoutBuf = stdoutBuf.slice(idx + 1);
      if (!line) continue;
      const m = line.match(/^MI_RIPPLE_READY\s+(\d+)$/);
      if (m && Number(m[1]) === port) {
        onServerReady();
        return;
      }
      log("[server]", line);
    }
  });
  serverProc.stderr.on("data", (chunk) => {
    serverLog += chunk;
    if (serverLog.length > 20000) serverLog = serverLog.slice(-20000);
    const line = String(chunk).trim();
    if (line) log("[server:err]", line);
  });
  serverProc.on("error", (err) => failServer(`Failed to start backend: ${err.message}`));
  serverProc.on("exit", (code, signal) => {
    if (!quitting) {
      failServer(
        `Backend exited unexpectedly (code=${code}, signal=${signal}).\n\n${tailLog()}`
      );
    }
  });

  readyTimer = setTimeout(() => failServer("Backend did not become ready in time.\n\n" + tailLog()), READY_TIMEOUT_MS);
}

function onServerReady() {
  if (readyTimer) clearTimeout(readyTimer);
  log(`backend ready on port ${appPort}`);
  showWindow();
}

function tailLog() {
  return (serverLog || "(no stderr output)").slice(-4000);
}

function failServer(message) {
  if (readyTimer) clearTimeout(readyTimer);
  log("backend failure:", message);
  dialog
    .showErrorBox(`${APP_TITLE} — startup error`, message)
    .catch(() => {});
  app.quit();
}

function stopServer() {
  if (!serverProc) return;
  const proc = serverProc;
  serverProc = null;
  if (proc.exitCode === null) {
    if (process.platform === "win32") {
      // Kill the whole process tree (python may spawn children).
      execFile("taskkill", ["/pid", String(proc.pid), "/T", "/F"], () => {});
    } else {
      try {
        proc.kill("SIGTERM");
      } catch (_) {}
      setTimeout(() => {
        try {
          if (proc.exitCode === null) proc.kill("SIGKILL");
        } catch (_) {}
      }, 4000).unref();
    }
  }
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------

function showWindow() {
  const url = `http://127.0.0.1:${appPort}/`;
  if (!mainWindow) {
    mainWindow = new BrowserWindow({
      width: 1380,
      height: 900,
      minWidth: 940,
      minHeight: 640,
      title: APP_TITLE,
      backgroundColor: "#08090a",
      autoHideMenuBar: true,
      icon: path.join(__dirname, "..", "assets", "icon.png"),
      webPreferences: {
        preload: path.join(__dirname, "preload.js"),
        contextIsolation: true,
        nodeIntegration: false,
        spellcheck: false,
      },
    });
    Menu.setApplicationMenu(null);

    const wc = mainWindow.webContents;
    wc.setWindowOpenHandler(() => ({ action: "deny" }));
    wc.on("will-navigate", (event, target) => {
      if (!target.startsWith(url)) event.preventDefault();
    });
    wc.on("render-process-gone", (_e, details) => {
      log("renderer gone:", details.reason);
    });
    mainWindow.on("closed", () => {
      mainWindow = null;
    });
    // Show a spinner while the Gradio bundle loads, then swap to the app.
    wc.loadURL("data:text/html," + encodeURIComponent(loadingHtml()));
  }
  mainWindow.loadURL(url);
  if (!mainWindow.isVisible()) mainWindow.show();

  // Headless self-test hook: `electron . --screenshot <path> [--wait <ms>]`
  const shotIdx = process.argv.indexOf("--screenshot");
  if (shotIdx >= 0 && process.argv[shotIdx + 1]) {
    const waitIdx = process.argv.indexOf("--wait");
    const waitMs = waitIdx >= 0 ? Number(process.argv[waitIdx + 1]) : 12000;
    setTimeout(async () => {
      try {
        const image = await mainWindow.webContents.capturePage();
        require("node:fs").writeFileSync(process.argv[shotIdx + 1], image.toPNG());
        const title = mainWindow.webContents.getTitle();
        console.log(`SCREENSHOT_SAVED title="${title}"`);
      } catch (err) {
        console.error("screenshot failed:", err.message);
      }
      stopServer();
      app.quit();
    }, waitMs);
  }
}

function loadingHtml() {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
    html,body{margin:0;height:100%;background:#08090a;color:#8a8f98;
      font:15px/1.6 Inter,system-ui,-apple-system,"Segoe UI",sans-serif;display:flex;
      align-items:center;justify-content:center;flex-direction:column;gap:18px}
    .rings{width:72px;height:72px;border-radius:50%;
      border:3px solid rgba(228,242,34,.22);border-top-color:#e4f222;
      animation:spin 1.1s linear infinite}
    @keyframes spin{to{transform:rotate(360deg)}}
    h1{font-size:20px;margin:0;color:#e5e5e6;font-weight:510;letter-spacing:-.012em}
    p{margin:0;font-size:13px;color:#62666d}
  </style></head><body>
    <div class="rings"></div>
    <h1>${APP_TITLE}</h1>
    <p>Starting the offline Gradio backend… (first launch can take a minute)</p>
  </body></html>`;
}

// ---------------------------------------------------------------------------
// Offline enforcement: block every request that is not our local backend.
// ---------------------------------------------------------------------------

function blockExternalRequests() {
  const filter = { urls: ["<all_urls>"] };
  session.defaultSession.webRequest.onBeforeRequest(filter, (details, callback) => {
    let host = "";
    try {
      host = new URL(details.url).hostname;
    } catch (_) {
      return callback({ cancel: true });
    }
    const local =
      host === "127.0.0.1" ||
      host === "localhost" ||
      host === "0.0.0.0" ||
      details.url.startsWith("data:") ||
      details.url.startsWith("blob:");
    callback({ cancel: !local });
  });
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    blockExternalRequests();
    try {
      appPort = await pickFreePort();
    } catch (err) {
      failServer(`Could not allocate a local port: ${err.message}`);
      return;
    }
    startServer(appPort);
  });

  app.on("before-quit", () => {
    quitting = true;
    stopServer();
  });
  app.on("window-all-closed", () => {
    stopServer();
    app.quit();
  });
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0 && appPort) showWindow();
  });
}
