import type { ElectronAPI } from '@electron-toolkit/preload'

declare global {
  interface Window {
    electron: ElectronAPI
    api: {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      getYaml(path: string): Promise<any>
      getMarkdown(path: string): Promise<string>
      hasYaml(path: string): Promise<boolean>
      hasMarkdown(path: string): Promise<boolean>
    }
  }
}
