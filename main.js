const { app, BrowserWindow, ipcMain, dialog } = require("electron");
const { spawn, spawnSync } = require("child_process");
const fs = require("fs");
const net = require("net");
const path = require("path");
const readline = require("readline");

let win = null;
let backend = null;
let readyPort = null;
const root = __dirname;
const APP_NAME = "壁纸生创流程台";

app.setName(APP_NAME);
// Keep the existing profile and single-instance lock when changing the display name.
app.setPath("userData", path.join(app.getPath("appData"), "公众号专项产出"));
if (process.platform === "win32") app.setAppUserModelId("WallpaperFlowStudio");

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const chosen = server.address().port;
      server.close(() => resolve(chosen));
    });
  });
}

function startBackend(port) {
  const dataDir = path.join(root, "data");
  fs.mkdirSync(dataDir, { recursive: true });
  const logPath = path.join(dataDir, "backend.log");
  const python = process.env.MP_CONTENT_PYTHON || "python";
  backend = spawn(python, ["-X", "utf8", "serve.py"], {
    cwd: root,
    windowsHide: true,
    env: {
      ...process.env,
      MP_CONTENT_PORT: String(port),
      MP_CONTENT_DESKTOP: "1",
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
    },
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend.stderr.on("data", (chunk) => fs.appendFileSync(logPath, chunk));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("本机服务启动超时")), 60000);
    const lines = readline.createInterface({ input: backend.stdout });
    lines.on("line", (line) => {
      fs.appendFileSync(logPath, line + "\n");
      try {
        const message = JSON.parse(line);
        if (message.ready) {
          readyPort = message.port;
          clearTimeout(timer);
          resolve();
        }
      } catch {
        // 人类可读的日志行跳过。
      }
    });
    backend.on("error", (error) => {
      clearTimeout(timer);
      reject(error);
    });
    backend.on("exit", (code) => {
      if (!readyPort) {
        clearTimeout(timer);
        reject(new Error("本机服务退出：" + code));
      }
    });
  });
}

function stopBackend() {
  if (!backend || backend.exitCode !== null) return;
  if (process.platform === "win32") {
    spawnSync("taskkill", ["/PID", String(backend.pid), "/T", "/F"], { windowsHide: true, stdio: "ignore" });
  } else {
    backend.kill();
  }
}

function showMainWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  if (!win.isVisible()) win.show();
  win.focus();
}

const ownsInstance = app.requestSingleInstanceLock();
if (!ownsInstance) app.quit();

app.on("second-instance", showMainWindow);

ipcMain.handle("window", (_event, action) => {
  if (!win) return;
  if (action === "minimize") win.minimize();
  if (action === "maximize") {
    if (win.isMaximized()) win.unmaximize();
    else win.maximize();
  }
  if (action === "close") win.close();
});

ipcMain.handle("pick-directory", async () => {
  const result = await dialog.showOpenDialog(win, {
    title: "选择保存文件夹",
    properties: ["openDirectory", "createDirectory"],
  });
  if (result.canceled || !result.filePaths[0]) return "";
  return result.filePaths[0];
});

if (ownsInstance) {
  app.whenReady().then(async () => {
    try {
      const port = await freePort();
      await startBackend(port);
      win = new BrowserWindow({
        width: 1480,
        height: 980,
        minWidth: 1080,
        minHeight: 720,
        show: false,
        backgroundColor: "#f4f6fb",
        frame: false,
        title: APP_NAME,
        icon: path.join(root, "assets", process.platform === "win32" ? "icon.ico" : "icon.png"),
        webPreferences: {
          preload: path.join(root, "preload.js"),
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
        },
      });
      win.once("ready-to-show", () => {
        win.show();
        win.focus();
      });
      win.on("closed", () => {
        win = null;
      });
      await win.loadURL(`http://127.0.0.1:${readyPort}/`);
    } catch (error) {
      dialog.showErrorBox(APP_NAME + "没有启动", String(error.message || error) + "\n日志：" + path.join(root, "data", "backend.log"));
      app.quit();
    }
  });
}

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => stopBackend());
