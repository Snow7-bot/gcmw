import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { RC_ALLOWED_EXTENSIONS, mimeTypeForRcFile, resolveAllowedPath } from './pathSecurity'

let root: string
let outsideDir: string | undefined

beforeEach(() => {
  root = fs.mkdtempSync(path.join(os.tmpdir(), 'gcmw-path-'))
})

afterEach(() => {
  fs.rmSync(root, { recursive: true, force: true })
  if (outsideDir) {
    fs.rmSync(outsideDir, { recursive: true, force: true })
    outsideDir = undefined
  }
})

describe('resolveAllowedPath', () => {
  it('allows legal files inside root', () => {
    fs.writeFileSync(path.join(root, 'a.md'), '# hi')
    fs.writeFileSync(path.join(root, 'b.yml'), 'x: 1')
    expect(resolveAllowedPath(root, 'a.md', ['.md'])).toBeTruthy()
    expect(resolveAllowedPath(root, 'b.yml', ['.yml'])).toBeTruthy()
  })

  it('rejects traversal and absolute external paths', () => {
    expect(resolveAllowedPath(root, '../outside.md', ['.md'])).toBeNull()
    expect(resolveAllowedPath(root, '/etc/passwd', ['.md'])).toBeNull()
  })

  it('rejects symlink escaping root', () => {
    outsideDir = path.join(path.dirname(root), `outside-${Date.now()}`)
    fs.mkdirSync(outsideDir)
    fs.writeFileSync(path.join(outsideDir, 'secret.md'), 'x')
    fs.symlinkSync(path.join(outsideDir, 'secret.md'), path.join(root, 'link.md'))
    expect(resolveAllowedPath(root, 'link.md', ['.md'])).toBeNull()
  })

  it('rejects .md symlink pointing to .env', () => {
    fs.writeFileSync(path.join(root, '.env'), 'SECRET=1')
    fs.symlinkSync(path.join(root, '.env'), path.join(root, 'evil.md'))
    expect(resolveAllowedPath(root, 'evil.md', ['.md'])).toBeNull()
  })

  it('rejects nonexistent files and disallowed extensions', () => {
    expect(resolveAllowedPath(root, 'missing.md', ['.md'])).toBeNull()
    fs.writeFileSync(path.join(root, 'page.html'), '<script>')
    fs.writeFileSync(path.join(root, 'app.js'), 'x')
    expect(resolveAllowedPath(root, 'page.html', RC_ALLOWED_EXTENSIONS)).toBeNull()
    expect(resolveAllowedPath(root, 'app.js', RC_ALLOWED_EXTENSIONS)).toBeNull()
  })

  it('allows images/videos and maps MIME', () => {
    fs.writeFileSync(path.join(root, 'a.png'), 'x')
    fs.writeFileSync(path.join(root, 'b.mp4'), 'x')
    expect(resolveAllowedPath(root, 'a.png', RC_ALLOWED_EXTENSIONS)).toBeTruthy()
    expect(resolveAllowedPath(root, 'b.mp4', RC_ALLOWED_EXTENSIONS)).toBeTruthy()
    expect(mimeTypeForRcFile(path.join(root, 'a.png'))).toBe('image/png')
    expect(mimeTypeForRcFile(path.join(root, 'b.mp4'))).toBe('video/mp4')
    expect(mimeTypeForRcFile(path.join(root, 'bad.exe'))).toBeNull()
  })
})
