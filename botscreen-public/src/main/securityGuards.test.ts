import type { IpcMainInvokeEvent } from 'electron'
import { describe, expect, it, vi } from 'vitest'
import {
  isTrustedAppUrl,
  isTrustedSenderFrame,
  registerTrustedIpc,
  type TrustedFrameLike,
  type TrustedIpcListener
} from './securityGuards'

const RENDERER_DIR = '/opt/app/out/renderer'

function packaged(url: string): boolean {
  return isTrustedAppUrl(url, { devUrl: undefined, rendererDir: RENDERER_DIR })
}

function topFrame(url: string): TrustedFrameLike {
  const frame: TrustedFrameLike = { url, top: null }
  frame.top = frame
  return frame
}

function subFrame(url: string, top: TrustedFrameLike): TrustedFrameLike {
  return { url, top }
}

function fakeIpcEvent(senderFrame: TrustedFrameLike | null): IpcMainInvokeEvent {
  return { senderFrame } as unknown as IpcMainInvokeEvent
}

describe('isTrustedAppUrl (dev server)', () => {
  const dev = (url: string): boolean =>
    isTrustedAppUrl(url, { devUrl: 'http://127.0.0.1:5173/', rendererDir: RENDERER_DIR })

  it('trusts any URL on the dev server origin', () => {
    expect(dev('http://127.0.0.1:5173/')).toBe(true)
    expect(dev('http://127.0.0.1:5173/#/qa')).toBe(true)
    expect(dev('http://127.0.0.1:5173/some/path?q=1')).toBe(true)
  })

  it('rejects other schemes, ports and hosts', () => {
    expect(dev('https://127.0.0.1:5173/')).toBe(false)
    expect(dev('http://127.0.0.1:5174/')).toBe(false)
    expect(dev('http://localhost:5173/')).toBe(false)
    expect(dev('file:///opt/app/out/renderer/index.html')).toBe(false)
  })

  it('rejects host-suffix lookalikes (string-prefix bypass)', () => {
    expect(dev('http://127.0.0.1:5173.evil.com/x')).toBe(false)
    expect(dev('http://127.0.0.1:51731/')).toBe(false)
  })

  it('rejects malformed URLs', () => {
    expect(dev('not a url')).toBe(false)
    expect(dev('')).toBe(false)
  })
})

describe('isTrustedAppUrl (packaged)', () => {
  it('trusts files under the renderer directory', () => {
    expect(packaged('file:///opt/app/out/renderer/index.html')).toBe(true)
    expect(packaged('file:///opt/app/out/renderer/index.html#/qa')).toBe(true)
    expect(packaged('file:///opt/app/out/renderer/assets/app.js?rev=1')).toBe(true)
    expect(packaged('file:///opt/app/out/renderer/assets/app.js')).toBe(true)
  })

  it('rejects the directory itself', () => {
    expect(packaged('file:///opt/app/out/renderer/')).toBe(false)
    expect(packaged('file:///opt/app/out/renderer')).toBe(false)
  })

  it('rejects plain dot-dot traversal (URL normalizes it before containment check)', () => {
    expect(packaged('file:///opt/app/out/renderer/../other/index.html')).toBe(false)
    expect(packaged('file:///opt/app/out/other/index.html')).toBe(false)
  })

  it('rejects percent-encoded dot-dot traversal', () => {
    expect(packaged('file:///opt/app/out/renderer/%2e%2e/other/index.html')).toBe(false)
    expect(packaged('file:///opt/app/out/renderer/%2E%2E%2Fother%2Findex.html')).toBe(false)
  })

  it('rejects encoded slashes', () => {
    expect(packaged('file:///opt/app/out/renderer%2F..%2Fsecret')).toBe(false)
  })

  it('rejects sibling directories and prefix lookalikes', () => {
    expect(packaged('file:///opt/app/out/renderer2/index.html')).toBe(false)
    expect(packaged('file:///opt/app/out/renderer_evil/index.html')).toBe(false)
    expect(packaged('file:///opt/app/out/other/index.html')).toBe(false)
  })

  it('rejects non-file schemes and remote URLs', () => {
    expect(packaged('http://evil.example.com/')).toBe(false)
    expect(packaged('https://evil.example.com/')).toBe(false)
    expect(packaged('rc://index.md')).toBe(false)
    expect(packaged('data:text/html,<script>x</script>')).toBe(false)
    expect(packaged('file:///etc/passwd')).toBe(false)
  })

  it('rejects file URLs with a foreign host', () => {
    expect(packaged('file://evil.example.com/opt/app/out/renderer/index.html')).toBe(false)
  })

  it('rejects malformed URLs', () => {
    expect(packaged('garbage')).toBe(false)
    expect(packaged('')).toBe(false)
  })
})

describe('isTrustedSenderFrame', () => {
  const trusted = (url: string): boolean => packaged(url)

  it('rejects a missing frame', () => {
    expect(isTrustedSenderFrame(null, trusted)).toBe(false)
  })

  it('accepts the top-level frame of the app document', () => {
    const frame = topFrame('file:///opt/app/out/renderer/index.html')
    expect(isTrustedSenderFrame(frame, trusted)).toBe(true)
  })

  it('rejects sub-frames even when their URL looks trusted', () => {
    const top = topFrame('file:///opt/app/out/renderer/index.html')
    const frame = subFrame('file:///opt/app/out/renderer/index.html', top)
    expect(isTrustedSenderFrame(frame, trusted)).toBe(false)
  })

  it('rejects top-level frames with an untrusted URL', () => {
    const frame = topFrame('file:///etc/passwd')
    expect(isTrustedSenderFrame(frame, trusted)).toBe(false)
  })
})

describe('registerTrustedIpc', () => {
  it('forwards invoke args to the handler only for a trusted top-frame sender', () => {
    let capturedChannel: string | undefined
    let capturedListener: TrustedIpcListener | undefined
    const registrar = {
      handle: (channel: string, listener: TrustedIpcListener): void => {
        capturedChannel = channel
        capturedListener = listener
      }
    }
    const handler = vi.fn((_event: IpcMainInvokeEvent, p: string) => `got:${p}`)
    const isTrustedUrl = (url: string): boolean => packaged(url)

    registerTrustedIpc(registrar, 'getmd', handler, isTrustedUrl)
    expect(capturedChannel).toBe('getmd')

    const trustedFrame = topFrame('file:///opt/app/out/renderer/index.html')
    const result = capturedListener?.(fakeIpcEvent(trustedFrame), 'note.md')
    expect(handler).toHaveBeenCalledTimes(1)
    expect(result).toBe('got:note.md')
  })

  it('throws and never calls the handler for an untrusted sender', () => {
    let capturedListener: TrustedIpcListener | undefined
    const registrar = {
      handle: (_channel: string, listener: TrustedIpcListener): void => {
        capturedListener = listener
      }
    }
    const handler = vi.fn()
    const isTrustedUrl = (url: string): boolean => packaged(url)

    registerTrustedIpc(registrar, 'getmd', handler, isTrustedUrl)

    expect(() =>
      capturedListener?.(fakeIpcEvent(topFrame('https://evil.example.com/')), 'note.md')
    ).toThrow('Unauthorized IPC sender')
    expect(() =>
      capturedListener?.(
        fakeIpcEvent(
          subFrame(
            'file:///opt/app/out/renderer/index.html',
            topFrame('file:///opt/app/out/renderer/index.html')
          )
        ),
        'note.md'
      )
    ).toThrow('Unauthorized IPC sender')
    expect(() => capturedListener?.(fakeIpcEvent(null), 'note.md')).toThrow(
      'Unauthorized IPC sender'
    )
    expect(handler).not.toHaveBeenCalled()
  })
})
