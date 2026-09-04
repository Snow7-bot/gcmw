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
  // @ts-ignore
  window.electron = electronAPI
  // @ts-ignore
  window.api = api
}
