import { contextBridge, ipcRenderer } from 'electron'
import { electronAPI } from '@electron-toolkit/preload'

const api = {
  getYaml: (path: string) => ipcRenderer.invoke('getyml', path),
  getMarkdown: (path: string) => ipcRenderer.invoke('getmd', path),
  hasYaml: (path: string) => ipcRenderer.invoke('hasyml', path),
  hasMarkdown: (path: string) => ipcRenderer.invoke('hasmd', path)
}

if (process.contextIsolated) {
  contextBridge.exposeInMainWorld('electron', electronAPI)
  contextBridge.exposeInMainWorld('api', api)
} else {
  // @ts-ignore - electronAPI types are not available in non-isolated context
  window.electron = electronAPI
  // @ts-ignore - api type is declared globally for renderer
  window.api = api
}
