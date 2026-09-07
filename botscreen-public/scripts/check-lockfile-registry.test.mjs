// Plain-JS CI helper tests (no TS type annotations).
/* eslint-disable @typescript-eslint/explicit-function-return-type */
import { describe, expect, it } from 'vitest'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const scriptPath = join(process.cwd(), 'scripts', 'check-lockfile-registry.mjs')
const NODE = process.execPath
const INTEGRITY =
  'sha512-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=='

function runCli(root) {
  try {
    const out = execFileSync(NODE, [scriptPath, '--root', root], { encoding: 'utf-8' })
    return { code: 0, stdout: out, stderr: '' }
  } catch (err) {
    return { code: err.status ?? 1, stdout: err.stdout ?? '', stderr: err.stderr ?? '' }
  }
}

function makeRoot(lock) {
  const root = mkdtempSync(join(tmpdir(), 'lockfile-reg-'))
  writeFileSync(
    join(root, 'package-lock.json'),
    typeof lock === 'string' ? lock : JSON.stringify(lock)
  )
  return root
}

const lockWith = (packages) => ({ name: 'x', version: '1.0.0', lockfileVersion: 3, packages })
const goodPkg = (resolved, integrity = INTEGRITY) => ({ resolved, integrity })

describe('strict registry whitelist', () => {
  it('accepts an all-official lockfile with integrity', () => {
    const root = makeRoot(
      lockWith({ 'node_modules/a': goodPkg('https://registry.npmjs.org/a/-/a-1.0.0.tgz') })
    )
    const res = runCli(root)
    expect(res.code).toBe(0)
    expect(res.stdout).toContain('lockfile-registry OK')
    rmSync(root, { recursive: true, force: true })
  })

  it.each([
    ['http url', 'http://registry.npmjs.org/a/-/a-1.0.0.tgz', 'non-https protocol'],
    ['ftp url', 'ftp://registry.npmjs.org/a/-/a-1.0.0.tgz', 'non-https protocol'],
    [
      'subdomain bypass',
      'https://registry.npmjs.org.evil.com/a/-/a-1.0.0.tgz',
      'source origin not approved'
    ],
    [
      'non-default port',
      'https://registry.npmjs.org:8443/a/-/a-1.0.0.tgz',
      'source origin not approved'
    ],
    ['mirror host', 'https://registry.npmmirror.com/a/-/a-1.0.0.tgz', 'source origin not approved'],
    ['malformed url', 'not a url at all', 'malformed resolved url'],
    [
      'missing integrity',
      'https://registry.npmjs.org/a/-/a-1.0.0.tgz',
      'missing or invalid integrity'
    ]
  ])('rejects %s', (_label, resolved, reason) => {
    const pkg = goodPkg(resolved)
    if (reason === 'missing or invalid integrity') delete pkg.integrity
    const root = makeRoot(lockWith({ 'node_modules/a': pkg }))
    const res = runCli(root)
    expect(res.code).toBe(1)
    expect(res.stderr).toContain('node_modules/a')
    expect(res.stderr).toContain(reason)
    rmSync(root, { recursive: true, force: true })
  })

  it('rejects credentials embedded in the URL', () => {
    const root = makeRoot(
      lockWith({
        'node_modules/a': goodPkg('https://user:sup3rsecret@registry.npmjs.org/a/-/a-1.0.0.tgz')
      })
    )
    const res = runCli(root)
    expect(res.code).toBe(1)
    expect(res.stderr).toContain('credentials embedded in url')
    rmSync(root, { recursive: true, force: true })
  })

  it.each([
    ['empty packages map', lockWith({})],
    ['missing packages key', { lockfileVersion: 3 }],
    ['null lockfile', null],
    ['malformed json', '{ not json']
  ])('fails on %s', (_label, lock) => {
    const root = makeRoot(lock)
    const res = runCli(root)
    expect(res.code).toBe(1)
    rmSync(root, { recursive: true, force: true })
  })

  it('sanitizes logs: never prints URLs, credentials or mirror names', () => {
    const root = makeRoot(
      lockWith({
        'node_modules/a': goodPkg('https://user:tok3n@registry.npmjs.org/a/-/a-1.0.0.tgz'),
        'node_modules/b': goodPkg('https://registry.npmmirror.com/b/-/b-1.0.0.tgz')
      })
    )
    const res = runCli(root)
    expect(res.code).toBe(1)
    const all = res.stdout + res.stderr
    expect(all).not.toContain('tok3n')
    expect(all).not.toContain('user:')
    expect(all).not.toContain('npmmirror')
    expect(all).not.toContain('registry.npmjs.org/')
    expect(all).toMatch(/node_modules\/a: credentials embedded in url/)
    expect(all).toMatch(/node_modules\/b: source origin not approved/)
    rmSync(root, { recursive: true, force: true })
  })
})
