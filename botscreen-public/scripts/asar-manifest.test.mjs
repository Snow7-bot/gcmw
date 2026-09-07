// Plain-JS CI helper tests (no TS type annotations).
/* eslint-disable @typescript-eslint/explicit-function-return-type */
import { describe, expect, it } from 'vitest'
import { createRequire } from 'node:module'
import { execFileSync } from 'node:child_process'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync, existsSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { pathToFileURL } from 'node:url'

const require = createRequire(import.meta.url)
const { createPackage } = require('@electron/asar')

const scriptPath = join(process.cwd(), 'scripts', 'asar-manifest.mjs')
const NODE = process.execPath

/** Build a fixture root with an app.asar made from `tree` (relPath -> content). */
function makeRoot() {
  const root = mkdtempSync(join(tmpdir(), 'asar-manifest-'))
  return root
}

let fixtureSeq = 0
async function packFixture(root, tree, asarRel = 'dist/one/resources/app.asar') {
  fixtureSeq += 1
  const src = join(root, 'fixture-src', String(fixtureSeq))
  for (const [rel, content] of Object.entries(tree)) {
    const file = join(src, rel)
    mkdirSync(dirname(file), { recursive: true })
    writeFileSync(file, content)
  }
  const dest = join(root, asarRel)
  mkdirSync(dirname(dest), { recursive: true })
  rmSync(dest, { force: true })
  await createPackage(src, dest)
  return dest
}

function runCli(root, args) {
  try {
    const out = execFileSync(NODE, [scriptPath, ...args], {
      env: { ...process.env, ASAR_MANIFEST_ROOT: root },
      encoding: 'utf-8'
    })
    return { code: 0, stdout: out }
  } catch (err) {
    return { code: err.status ?? 1, stdout: err.stdout ?? '', stderr: err.stderr ?? '' }
  }
}

const pkg = (name, version) => JSON.stringify({ name, version, main: 'index.js' }, null, 0) + '\n'

describe('collectModuleManifest', () => {
  it('lists real packages as name@version and skips scope containers', async () => {
    const root = makeRoot()
    const asar = await packFixture(root, {
      'node_modules/foo/package.json': pkg('foo', '1.0.0'),
      'node_modules/@scope/bar/package.json': pkg('@scope/bar', '2.1.0'),
      'node_modules/@vue/compiler-core/package.json': pkg('@vue/compiler-core', '3.5.0'),
      'out/index.js': 'x'
    })
    const { collectModuleManifest } = await awaitImport()
    const manifest = collectModuleManifest(asar)
    expect(manifest).toEqual(['@scope/bar@2.1.0', '@vue/compiler-core@3.5.0', 'foo@1.0.0'])
    expect(manifest.some((l) => l.startsWith('@vue@') || l === '@scope')).toBe(false)
    rmSync(root, { recursive: true, force: true })
  })

  it('fails on a module without package.json', async () => {
    const root = makeRoot()
    const asar = await packFixture(root, {
      'node_modules/broken/readme.txt': 'no package.json here'
    })
    const { collectModuleManifest } = await awaitImport()
    expect(() => collectModuleManifest(asar)).toThrow(/has no package\.json/)
    rmSync(root, { recursive: true, force: true })
  })

  it('fails on a corrupt package.json', async () => {
    const root = makeRoot()
    const asar = await packFixture(root, {
      'node_modules/broken/package.json': '{not json'
    })
    const { collectModuleManifest } = await awaitImport()
    expect(() => collectModuleManifest(asar)).toThrow(/corrupt package\.json/)
    rmSync(root, { recursive: true, force: true })
  })
})

describe('checkForbiddenPaths', () => {
  it('flags server/, scripts/, .env and key material', async () => {
    const root = makeRoot()
    const asar = await packFixture(root, {
      'server/.env.example': 'SECRET=1',
      'server/app/main.py': 'x',
      'scripts/leak.sh': 'x',
      'keystore.pem': 'PRIVATE',
      'out/index.js': 'ok'
    })
    const { checkForbiddenPaths } = await awaitImport()
    const v = checkForbiddenPaths(asar)
    expect(v.join('\n')).toContain('server')
    expect(v.join('\n')).toContain('scripts')
    expect(v.join('\n')).toContain('.env')
    expect(v.join('\n')).toContain('keystore.pem')
    rmSync(root, { recursive: true, force: true })
  })
})

describe('CLI gates', () => {
  it('--check fails (no auto-create) when the baseline is missing', async () => {
    const root = makeRoot()
    await packFixture(root, { 'node_modules/foo/package.json': pkg('foo', '1.0.0') })
    const res = runCli(root, ['--check'])
    expect(res.code).toBe(1)
    expect(res.stderr).toContain('baseline missing')
    expect(res.stderr).toContain('--write')
    expect(existsSync(join(root, 'packaging', 'app.asar.modules.txt'))).toBe(false)
    rmSync(root, { recursive: true, force: true })
  })

  it('--write creates the baseline; version change makes --check fail', async () => {
    const root = makeRoot()
    await packFixture(root, { 'node_modules/foo/package.json': pkg('foo', '1.0.0') })
    expect(runCli(root, ['--write']).code).toBe(0)
    expect(runCli(root, ['--check']).code).toBe(0)

    // same package name, vulnerable downgrade 1.0.0 -> 0.9.0
    await packFixture(root, { 'node_modules/foo/package.json': pkg('foo', '0.9.0') })
    const res = runCli(root, ['--check'])
    expect(res.code).toBe(1)
    expect(res.stderr).toContain('+ foo@0.9.0')
    expect(res.stderr).toContain('- foo@1.0.0')
    rmSync(root, { recursive: true, force: true })
  })

  it('fails on multiple app.asar; --asar pins one archive', async () => {
    const root = makeRoot()
    await packFixture(
      root,
      { 'node_modules/foo/package.json': pkg('foo', '1.0.0') },
      'dist/a/resources/app.asar'
    )
    await packFixture(
      root,
      { 'node_modules/bar/package.json': pkg('bar', '1.0.0') },
      'dist/b/resources/app.asar'
    )
    const res = runCli(root, ['--check'])
    expect(res.code).toBe(2)
    expect(res.stderr).toContain('multiple app.asar')
    const pinned = runCli(root, ['--write', '--asar', join(root, 'dist/b/resources/app.asar')])
    expect(pinned.code).toBe(0)
    const baseline = readFileSync(join(root, 'packaging', 'app.asar.modules.txt'), 'utf-8')
    expect(baseline).toContain('bar@1.0.0')
    expect(baseline).not.toContain('foo@')
    rmSync(root, { recursive: true, force: true })
  })

  it('--check refuses a leaking asar even if the baseline exists', async () => {
    const root = makeRoot()
    await packFixture(root, { 'node_modules/foo/package.json': pkg('foo', '1.0.0') })
    expect(runCli(root, ['--write']).code).toBe(0)
    await packFixture(root, {
      'node_modules/foo/package.json': pkg('foo', '1.0.0'),
      'server/.env.production': 'TOKEN=secret'
    })
    const res = runCli(root, ['--check'])
    expect(res.code).toBe(1)
    expect(res.stderr).toContain('packaging contract violated')
    expect(res.stderr).toContain('server')
    rmSync(root, { recursive: true, force: true })
  })
})

async function awaitImport() {
  return await import(pathToFileURL(join(process.cwd(), 'scripts', 'asar-manifest.mjs')).href)
}
