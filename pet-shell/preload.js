// Minimal, context-isolated bridge for window dragging.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("petWindow", {
  dragStart: () => ipcRenderer.send("pet:drag-start"),
  dragMove: () => ipcRenderer.send("pet:drag-move"),
  dragEnd: () => ipcRenderer.send("pet:drag-end"),
  setOpacity: (value) => ipcRenderer.send("pet:opacity", value),
  setIgnore: (ignore) => ipcRenderer.send("pet:ignore", ignore),
  setShellSize: (width, height) => ipcRenderer.send("pet:shell-size", { width, height }),
  contextMenu: (payload) => ipcRenderer.send("pet:context-menu", payload),
  onCommand: (callback) => {
    ipcRenderer.on("pet:command", (_event, command) => callback(String(command)));
  },
  onVisibility: (callback) => {
    ipcRenderer.on("pet:visibility", (_event, visible) => callback(Boolean(visible)));
  },
});
