import { contextBridge, ipcRenderer } from 'electron'

// Minimal, frozen API surface. Sandboxed preloads may only load Electron's
// built-in renderer modules, so this file must stay free of any third-party
// or node: imports — electron-vite keeps dependencies external, and sandboxed
// preloads cannot load them.
const api = {
  getYaml: (path: string) => ipcRenderer.invoke('getyml', path),
  getMarkdown: (path: string) => ipcRenderer.invoke('getmd', path),
  hasYaml: (path: string) => ipcRenderer.invoke('hasyml', path),
  hasMarkdown: (path: string) => ipcRenderer.invoke('hasmd', path)
}

contextBridge.exposeInMainWorld('api', Object.freeze(api))
