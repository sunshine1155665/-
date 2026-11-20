// Local whale-girl desktop shell.
// A transparent, always-on-top Electron window that renders the Live2D model
// and talks to the home pet gateway (ws://127.0.0.1:9950/local).
const { app, BrowserWindow, Menu, Tray, nativeImage, screen, ipcMain } = require("electron");
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..");
const TOKEN_KEYS = ["PET_GATEWAY_TOKEN"];

function readToken() {
  if (process.env.PET_GATEWAY_TOKEN) {
    return String(process.env.PET_GATEWAY_TOKEN).trim();
  }
  const envFile = path.join(ROOT, "data", "run", "media_tokens.env");
  try {
    for (const line of fs.readFileSync(envFile, "utf8").split(/\r?\n/)) {
      const index = line.indexOf("=");
      if (index > 0 && line.slice(0, index).trim() === TOKEN_KEYS[0]) {
        return line.slice(index + 1).trim();
      }
    }
  } catch (_) {
    /* no token file: the shell will show the offline state */
  }
  return "";
}

let win = null;
let tray = null;
let token = "";
let shellInfo = null;
let winOpacity = 1.0;
let dragState = null;
let baseWindowSize = null;

function setOpacity(value) {
  winOpacity = Math.max(0.2, Math.min(1.0, Number(value) || 1));
  if (win) win.setOpacity(winOpacity);
}

function installDragIpc() {
  ipcMain.on("pet:drag-start", () => {
    if (!win) return;
    const point = screen.getCursorScreenPoint();
    const [x, y] = win.getPosition();
    const [width, height] = win.getSize();
    dragState = { cursorX: point.x, cursorY: point.y, winX: x, winY: y, width, height };
    // Frameless transparent windows on Windows can grow while being moved via
    // setPosition.  The window is non-resizable; setBounds below freezes the
    // width/height for the whole drag.
    shellLog(`drag-start cursor=${point.x},${point.y} win=${x},${y} size=${width}x${height}`);
  });
  ipcMain.on("pet:drag-move", () => {
    if (!win || !dragState) return;
    const point = screen.getCursorScreenPoint();
    let nextX = dragState.winX + (point.x - dragState.cursorX);
    let nextY = dragState.winY + (point.y - dragState.cursorY);
    // Keep the whole shell inside the work area.  A transparent topmost
    // window dragged half off-screen used to keep eating clicks while the
    // visible part looked harmless.
    const area = screen.getDisplayNearestPoint(point).workArea;
    const maxX = area.x + Math.max(0, area.width - dragState.width);
    const maxY = area.y + Math.max(0, area.height - dragState.height);
    nextX = Math.max(area.x, Math.min(maxX, nextX));
    nextY = Math.max(area.y, Math.min(maxY, nextY));
    // setPosition alone was observed to make the window grow by 1px per move
    // on Windows.  setBounds with an explicit frozen size avoids that.
    win.setBounds({
      x: Math.round(nextX),
      y: Math.round(nextY),
      width: dragState.width,
      height: dragState.height,
    });
  });
  ipcMain.on("pet:drag-end", () => {
    if (dragState && win) {
      const [x, y] = win.getPosition();
      const [width, height] = win.getSize();
      shellLog(`drag-end win=${x},${y} size=${width}x${height}`);
      if (width !== dragState.width || height !== dragState.height) {
        win.setBounds({
          x,
          y,
          width: dragState.width,
          height: dragState.height,
        });
      }
    }
    dragState = null;
  });
  ipcMain.on("pet:opacity", (_event, value) => {
    setOpacity(value);
    shellLog(`opacity=${winOpacity.toFixed(2)}`);
  });
  ipcMain.on("pet:shell-size", (_event, size) => {
    if (!win || !baseWindowSize || !size) return;
    const bounds = win.getBounds();
    const area = screen.getDisplayMatching(bounds).workArea;
    const width = Math.max(
      240,
      Math.min(area.width, Math.round(Number(size.width) || bounds.width))
    );
    const height = Math.max(
      320,
      Math.min(area.height, Math.round(Number(size.height) || bounds.height))
    );
    const x = bounds.x + bounds.width - width;
    const y = bounds.y + bounds.height - height;
    win.setSize(width, height);
    win.setPosition(
      Math.max(area.x, Math.min(area.x + area.width - width, x)),
      Math.max(area.y, Math.min(area.y + area.height - height, y))
    );
    shellLog(`shell-size requested=${size.width}x${size.height} actual=${width}x${height}`);
  });
  ipcMain.on("pet:ignore", (_event, ignore) => {
    if (!win) return;
    // Transparent pixels must not eat clicks for the rest of the desktop.
    // forward:true keeps mousemove events flowing so the renderer can turn
    // this off as soon as the cursor is over the model or the controls.
    win.setIgnoreMouseEvents(Boolean(ignore), { forward: true });
    shellLog(`ignore=${Boolean(ignore) ? 1 : 0}`);
  });
  ipcMain.on("pet:context-menu", () => {
    showContextMenu();
  });
}

function sendPetCommand(command) {
  if (win) win.webContents.send("pet:command", command);
}

function showContextMenu() {
  if (!win) return;
  const opacityItems = [100, 90, 80, 70, 60, 50].map((value) => ({
    label: `${value}%`,
    click: () => setOpacity(value / 100),
  }));
  const menu = Menu.buildFromTemplate([
    {
      label: "动作",
      submenu: [
        { label: "摸摸头", click: () => sendPetCommand("action:head") },
        { label: "戳一戳", click: () => sendPetCommand("action:poke") },
        { label: "开心", click: () => sendPetCommand("action:happy") },
        { label: "害羞", click: () => sendPetCommand("action:shy") },
        { label: "惊讶", click: () => sendPetCommand("action:surprised") },
        { label: "双击彩蛋", click: () => sendPetCommand("action:dblclick") },
      ],
    },
    {
      label: "场景",
      submenu: [
        { label: "默认桌面", click: () => sendPetCommand("scene:default") },
        { label: "工作陪伴", click: () => sendPetCommand("scene:work") },
        { label: "干饭时间", click: () => sendPetCommand("scene:meal") },
        { label: "撒娇贴贴", click: () => sendPetCommand("scene:clingy") },
        { label: "开心庆祝", click: () => sendPetCommand("scene:happy") },
        { label: "听歌时刻", click: () => sendPetCommand("scene:music") },
        { label: "深夜陪伴", click: () => sendPetCommand("scene:night") },
        { label: "慵懒早晨", click: () => sendPetCommand("scene:morning") },
        { label: "追剧时刻", click: () => sendPetCommand("scene:drama") },
        { label: "小恶魔（彩蛋）", click: () => sendPetCommand("scene:devil") },
      ],
    },
    { type: "separator" },
    {
      label: "大小",
      submenu: [
        { label: "放大", click: () => sendPetCommand("zoom:in") },
        { label: "缩小", click: () => sendPetCommand("zoom:out") },
        { label: "100%", click: () => sendPetCommand("zoom:reset") },
      ],
    },
    { label: "不透明度", submenu: opacityItems },
    {
      label: "回到右下角",
      click: () => {
        if (!win) return;
        const d = screen.getPrimaryDisplay().workArea;
        const [w, h] = win.getSize();
        win.setPosition(d.x + d.width - w - 24, d.y + d.height - h - 12);
      },
    },
    {
      label: "置顶",
      type: "checkbox",
      checked: win.isAlwaysOnTop(),
      click: () => win.setAlwaysOnTop(!win.isAlwaysOnTop(), "screen-saver"),
    },
    { type: "separator" },
    { label: "退出鲸鱼娘", click: () => app.quit() },
  ]);
  menu.popup({ window: win });
}

let logDirReady = false;
function ensureLogDir() {
  if (logDirReady) return;
  try {
    fs.mkdirSync(path.join(ROOT, "data", "logs"), { recursive: true });
    logDirReady = true;
  } catch (_) {}
}

function shellLog(message) {
  ensureLogDir();
  try {
    fs.appendFileSync(
      path.join(ROOT, "data", "logs", "pet-shell-main.log"),
      `${new Date().toISOString()} ${message}\n`
    );
  } catch (_) {}
}

// Portable mode: outside the home setup there is no scripts/pet_shell_server.py,
// so the Electron main process serves the shell itself.  The home machine
// pre-starts the python server on 9951 and the probe below finds it first,
// which keeps the /tts proxy and existing behaviour unchanged there.
const APP_ROOT = __dirname;
let embeddedServer = null;

function startEmbeddedShellServer(port) {
  const http = require("http");
  const MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".moc3": "application/octet-stream",
    ".wasm": "application/wasm",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp3": "audio/mpeg",
  };
  const server = http.createServer((req, res) => {
    try {
      const urlPath = decodeURIComponent((req.url || "/").split("?")[0]);
      if (urlPath === "/tts") {
        // same-origin proxy to the local TTS service (mirrors pet_shell_server)
        const body = [];
        req.on("data", (c) => body.push(c));
        req.on("end", () => {
          const upstream = http.request(
            { host: "127.0.0.1", port: 9880, path: "/tts", method: "POST", timeout: 300 },
            (up) => {
              res.writeHead(up.statusCode || 502, { "Content-Type": up.headers["content-type"] || "audio/wav", "Cache-Control": "no-store" });
              up.pipe(res);
            }
          );
          upstream.on("error", () => {
            try { res.writeHead(503, { "Content-Type": "application/json" }); res.end(JSON.stringify({ ok: false, error: "tts unavailable" })); } catch (_) {}
          });
          upstream.write(Buffer.concat(body));
          upstream.end();
        });
        return;
      }
      let filePath = path.normalize(path.join(APP_ROOT, urlPath));
      if (filePath !== APP_ROOT && !filePath.startsWith(APP_ROOT + path.sep)) {
        res.writeHead(403);
        res.end();
        return;
      }
      if (fs.existsSync(filePath) && fs.statSync(filePath).isDirectory()) {
        filePath = path.join(filePath, "index.html");
      }
      if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) {
        res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
        res.end("not found");
        return;
      }
      const ext = path.extname(filePath).toLowerCase();
      res.writeHead(200, {
        "Content-Type": MIME[ext] || "application/octet-stream",
        "Cache-Control": "no-store",
        "Access-Control-Allow-Origin": "*",
      });
      fs.createReadStream(filePath).pipe(res);
    } catch (_) {
      try {
        res.writeHead(500);
        res.end();
      } catch (_) {}
    }
  });
  return new Promise((resolve) => {
    server.once("error", () => resolve(null));
    server.listen(port, "127.0.0.1", () => resolve(server));
  });
}

function probeExternalShell(port) {
  const http = require("http");
  return new Promise((resolve) => {
    const req = http.get(
      { host: "127.0.0.1", port, path: "/renderer/index.html", timeout: 700 },
      (res) => {
        res.resume();
        resolve(res.statusCode === 200);
      }
    );
    req.on("timeout", () => {
      req.destroy();
      resolve(false);
    });
    req.on("error", () => resolve(false));
  });
}

async function resolveShellUrl() {
  const port = 9951;
  if (process.env.PET_SHELL_URL) return { url: process.env.PET_SHELL_URL, portable: false };
  // retry: a slow python bind (AV scan, cold start) must not silently cost
  // the external server its /tts proxy
  for (let i = 0; i < 3; i++) {
    if (await probeExternalShell(port)) {
      return { url: `http://127.0.0.1:${port}/renderer/index.html`, portable: false };
    }
    await new Promise((resolve) => setTimeout(resolve, 400));
  }
  embeddedServer = await startEmbeddedShellServer(port);
  if (embeddedServer) shellLog("embedded shell server started on 9951");
  return { url: `http://127.0.0.1:${port}/renderer/index.html`, portable: true };
}

function createWindow(shellUrl, portable) {
  const display = screen.getPrimaryDisplay().workArea;
  const width = Math.min(460, Math.max(360, Math.round(display.width * 0.28)));
  // Leave room for the Windows taskbar; a 0.82*height window can exceed the
  // physical screen on 125%/150% DPI laptops and put its top off-screen.
  const height = Math.min(640, Math.max(420, display.height - 100));
  win = new BrowserWindow({
    width,
    height,
    x: display.x + display.width - width - 24,
    y: display.y + display.height - height - 12,
    transparent: true,
    frame: false,
    resizable: false,
    hasShadow: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    backgroundColor: "#00000000",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false,
      zoomFactor: 1,
      preload: path.join(__dirname, "preload.js"),
    },
  });
  win.setAlwaysOnTop(true, "screen-saver");
  win.setMenuBarVisibility(false);
  baseWindowSize = win.getSize();
  // Reset Chromium page zoom.  It persists per origin in Electron; earlier
  // Ctrl+- tests left the page at ~54% zoom, which made innerWidth small and
  // made "Ctrl+-" look like it only changed text size.
  try {
    win.webContents.setZoomFactor(1);
    win.webContents.setVisualZoomLevelLimits(1, 1);
  } catch (_) {}
  win.webContents.on("zoom-changed", (event, zoomDirection) => {
    if (event && event.preventDefault) event.preventDefault();
    let factor = 1;
    try {
      factor = win.webContents.getZoomFactor();
    } catch (_) {}
    shellLog(`zoom-changed dir=${zoomDirection || "?"} before-reset=${factor}`);
    try {
      win.webContents.setZoomFactor(1);
      win.webContents.setZoomLevel(0);
    } catch (_) {}
  });
  // A stale page zoom makes the entire DOM grow while our percentage label
  // stays unchanged.  Keep it pinned; all intentional zoom is the shell size.
  setInterval(() => {
    if (!win || win.isDestroyed()) return;
    try {
      const factor = win.webContents.getZoomFactor();
      if (Math.abs(factor - 1) > 0.001) {
        shellLog(`zoom-watchdog factor=${factor}`);
        win.webContents.setZoomFactor(1);
      }
    } catch (_) {}
  }, 500);
  win.once("ready-to-show", () => {
    win.showInactive();
    win.moveTop();
    // Start click-through. The renderer flips this off when the cursor is
    // over the model, controls, input or bubble.
    win.setIgnoreMouseEvents(true, { forward: true });
  });
  win.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    if (
      input.control &&
      !input.alt &&
      ["=", "+", "-", "_", "0"].includes(String(input.key))
    ) {
      // Keep Ctrl+= / Ctrl+- as the pet's window-size shortcuts and stop
      // Chromium from applying its own page zoom at the same time.
      event.preventDefault();
      if (input.key === "0") sendPetCommand("zoom:reset");
      else if (input.key === "-" || input.key === "_") sendPetCommand("zoom:out");
      else sendPetCommand("zoom:in");
      return;
    }
    if (!input.control || !input.alt) return;
    if (input.key === "ArrowUp") {
      event.preventDefault();
      setOpacity(winOpacity + 0.1);
    } else if (input.key === "ArrowDown") {
      event.preventDefault();
      setOpacity(winOpacity - 0.1);
    } else if (input.key === "0") {
      event.preventDefault();
      setOpacity(1);
    }
  });
  const wsParam = portable ? "none" : "ws://127.0.0.1:9950/local";
  win.loadURL(
    `${shellUrl}?token=${encodeURIComponent(token)}&ws=${encodeURIComponent(
      wsParam
    )}&baseW=${baseWindowSize[0]}&baseH=${baseWindowSize[1]}`
  );
  win.webContents.on("console-message", (_event, level, message, line, sourceId) => {
    try {
      fs.appendFileSync(
        path.join(ROOT, "data", "logs", "pet-shell-renderer.log"),
        `${new Date().toISOString()} [${level}] ${message} (${sourceId}:${line})\n`
      );
    } catch (_) {}
  });
  win.webContents.on("did-fail-load", (_event, code, desc) => {
    try {
      fs.appendFileSync(
        path.join(ROOT, "data", "logs", "pet-shell-renderer.log"),
        `${new Date().toISOString()} did-fail-load ${code} ${desc}\n`
      );
    } catch (_) {}
  });
  win.webContents.on("did-finish-load", () => {
    try {
      win.webContents.setZoomFactor(1);
      win.webContents.setZoomLevel(0);
    } catch (_) {}
    baseWindowSize = win.getSize();
    setTimeout(async () => {
      try {
        const image = await win.webContents.capturePage();
        fs.writeFileSync(
          path.join(ROOT, "data", "logs", "pet-shell-capture.png"),
          image.toPNG()
        );
      } catch (_) {}
    }, 5000);
  });
  win.on("show", () => { try { win.webContents.send("pet:visibility", true); } catch (_) {} });
  win.on("hide", () => { try { win.webContents.send("pet:visibility", false); } catch (_) {} });
  win.on("closed", () => {
    win = null;
  });
  win.on("resize", () => {
    if (!win) return;
    const [width, height] = win.getSize();
    shellLog(`window-resize ${width}x${height}`);
    if (
      dragState &&
      (width !== dragState.width || height !== dragState.height)
    ) {
      const [x, y] = win.getPosition();
      win.setBounds({
        x,
        y,
        width: dragState.width,
        height: dragState.height,
      });
    }
  });
}

function createTray() {
  const iconPath = path.join(__dirname, "assets", "model", "icon.png");
  let icon = nativeImage.createEmpty();
  try {
    icon = nativeImage.createFromPath(iconPath).resize({ width: 32, height: 32 });
  } catch (_) {
    /* tray icon is optional */
  }
  tray = new Tray(icon);
  tray.setToolTip("鲸鱼娘 · 桌面");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: "显示 / 隐藏",
        click: () => {
          if (!win) return;
          if (win.isVisible()) win.hide();
          else win.show();
        },
      },
      {
        label: "回到右下角",
        click: () => {
          if (!win) return;
          const d = screen.getPrimaryDisplay().workArea;
          const [w, h] = win.getSize();
          win.setPosition(d.x + d.width - w - 24, d.y + d.height - h - 12);
        },
      },
      {
        label: "不透明度",
        submenu: [
          { label: "100%", click: () => setOpacity(1.0) },
          { label: "90%", click: () => setOpacity(0.9) },
          { label: "80%", click: () => setOpacity(0.8) },
          { label: "70%", click: () => setOpacity(0.7) },
        ],
      },
      {
        label: "缩放提示",
        enabled: false,
      },
      { type: "separator" },
      {
        label: "退出",
        click: () => {
          app.quit();
        },
      },
    ]),
  );
  tray.on("click", () => {
    if (!win) return;
    if (win.isVisible()) win.hide();
    else win.show();
  });
}

// Portable copies (no scripts/ neighbor) get their own userData so the
// single-instance lock and localStorage never collide with the home install.
const PORTABLE = !fs.existsSync(path.join(ROOT, "scripts"));
if (PORTABLE) {
  app.setPath("userData", path.join(ROOT, "tools", "userdata"));
}

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      win.show();
      win.focus();
    }
  });
  app.whenReady().then(() => {
    installDragIpc();
    screen.on("display-metrics-changed", (_event, display, changedMetrics) => {
      shellLog(
        `display-metrics-changed scale=${display.scaleFactor} metrics=${(changedMetrics || []).join(",")}`
      );
    });
    token = readToken();
    resolveShellUrl().then((info) => {
      shellInfo = info;
      createWindow(info.url, info.portable);
    });
    try {
      createTray();
    } catch (error) {
      console.error("tray failed", error);
    }
    app.on("activate", () => {
      if (BrowserWindow.getAllWindows().length === 0) {
        if (shellInfo) createWindow(shellInfo.url, shellInfo.portable);
        else resolveShellUrl().then((info) => { shellInfo = info; createWindow(info.url, info.portable); });
      }
    });
  });
  app.on("window-all-closed", () => {
    app.quit();
  });
}
