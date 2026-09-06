import { isAbsolute, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import type { IpcMainInvokeEvent } from 'electron'

/**
 * Pure, unit-testable security guards for the Electron main process.
 * No electron runtime import — this module runs in plain Node under vitest.
 */

/** Structural subset of Electron's WebFrameMain needed by the guards. */
export interface TrustedFrameLike {
  url: string
  top: TrustedFrameLike | null
}

export interface TrustedOriginConfig {
  /**
   * Dev server URL. Must only be set when the app actually loads from the dev
   * server (is.dev && ELECTRON_RENDERER_URL). When set, only same-origin URLs
   * are trusted.
   */
  devUrl?: string
  /** Absolute path of the packaged renderer output directory (out/renderer). */
  rendererDir: string
}

/**
 * True when a URL belongs to this app's own renderer origin.
 *
 * Packaged mode: parse with `new URL` (normalizes plain `..` segments and
 * enforces a file: scheme), decode with `fileURLToPath` (rejects encoded
 * slashes), then verify containment with `path.relative` (rejects any
 * remaining `..` from percent-encoded dots, host tricks like
 * file://evil-host/..., and sibling paths such as renderer2/).
 */
export function isTrustedAppUrl(url: string, config: TrustedOriginConfig): boolean {
  try {
    if (config.devUrl) {
      // Dev server: compare origins, never string prefixes —
      // "http://127.0.0.1:5173.evil.com" must not match "http://127.0.0.1:5173".
      return new URL(url).origin === new URL(config.devUrl).origin
    }

    const parsed = new URL(url)
    if (parsed.protocol !== 'file:') return false
    const filePath = fileURLToPath(parsed) // throws on encoded slashes / non-local hosts
    const rel = relative(config.rendererDir, filePath)
    return rel !== '' && rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel)
  } catch {
    return false
  }
}

/** Only the top-level document carries our preload; reject IPC from sub-frames. */
export function isTrustedSenderFrame(
  frame: TrustedFrameLike | null,
  isTrustedUrl: (url: string) => boolean
): boolean {
  return frame !== null && frame === frame.top && isTrustedUrl(frame.url)
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- mirrors ipcMain.handle's listener signature
export type TrustedIpcListener = (event: IpcMainInvokeEvent, ...args: any[]) => unknown

/** Structural subset of Electron's IpcMain (ipcMain.handle). */
export interface IpcMainHandleLike {
  handle(channel: string, listener: TrustedIpcListener): void
}

/**
 * ipcMain.handle wrapper that rejects any invoke whose sender frame is not the
 * trusted top-level document (validated via isTrustedSenderFrame + isTrustedAppUrl).
 */
export function registerTrustedIpc(
  ipc: IpcMainHandleLike,
  channel: string,
  handler: TrustedIpcListener,
  isTrustedUrl: (url: string) => boolean
): void {
  ipc.handle(channel, (event, ...args) => {
    if (!isTrustedSenderFrame(event.senderFrame, isTrustedUrl)) {
      throw new Error('Unauthorized IPC sender')
    }
    return handler(event, ...args)
  })
}
