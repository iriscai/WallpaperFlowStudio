const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("desktop", {
  window: (action) => ipcRenderer.invoke("window", action),
  pickDirectory: () => ipcRenderer.invoke("pick-directory"),
});
