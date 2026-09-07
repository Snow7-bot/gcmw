// Plain-JS CI helper (no TS type annotations).
/* eslint-disable @typescript-eslint/explicit-function-return-type */
// app.asar SCA gate:
//  - lists every real top-level node_modules package as name@version (parsed
//    from the package.json INSIDE the asar; scope containers like @types are
//    not packages and are skipped; a module without a readable package.json is
//    an error);
//  - enforces the packaging contract: no /server, /scripts, .env* or
//    certificate/private-key material may ship in the asar;
//  - --check fails when the committed baseline is missing (no auto-create) and
//    when manifest drift or contract violations are found;
//  - --write (re)creates the baseline, but only after the contract passes;
//  - --asar <path> pins the archive; without it, finding more than one app.asar
//    under dist/ is an error (stale platform artifacts must not be checked).
//
// Usage:
//   node scripts/asar-manifest.mjs                    # print name@version list
//   node scripts/asar-manifest.mjs --write            # (re)create baseline
//   node scripts/asar-manifest.mjs --check            # CI gate
//   node scripts/asar-manifest.mjs --check --asar <p> # pin archive (debug)
import { createRequire } from 'node:module'
import { existsSync, readFileSync, writeFileSync, readdirSync, statSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { listPackage, extractFile } = require('@electron/asar')

const ROOT_ENV = process.env.ASAR_MANIFEST_ROOT
const root = ROOT_ENV ? ROOT_ENV : join(dirname(fileURLToPath(import.meta.url)), '..')
const distDir = join(root, 'dist')
const baselineFile = join(root, 'packaging', 'app.asar.modules.txt')

const FORBIDDEN_TOP_LEVEL = new Set(['server', 'scripts'])
const SECRET_BASENAME_RE =
  /^(\.env(\..*)?|.*\.(pem|key|p12|pfx|cer|crt|der)$|id_rsa|id_ecdsa|id_ed25519)$/i

/** All app.asar files under dist/ (explicit path returns that single one). */
export function findAsars(asarArg) {
  if (asarArg) {
    if (!existsSync(asarArg)) throw new Error(`asar not found: ${asarArg}`)
    return [asarArg]
  }
  if (!existsSync(distDir)) throw new Error(`dist/ missing — run "npm run build:unpack" first`)
  const found = []
  const queue = [distDir]
  while (queue.length > 0) {
    const dir = queue.shift()
    let entries
    try {
      entries = readdirSync(dir)
    } catch {
      continue
    }
    for (const name of entries) {
      const full = join(dir, name)
      let st
      try {
        st = statSync(full)
      } catch {
        continue
      }
      if (name === 'app.asar' && st.isFile()) found.push(full)
      else if (st.isDirectory() && name !== 'node_modules') queue.push(full)
    }
  }
  if (found.length === 0)
    throw new Error('no app.asar found under dist/ — run "npm run build:unpack"')
  if (found.length > 1) {
    throw new Error(
      `multiple app.asar found (${found.length}); pass --asar <path> to pin one:\n  ${found.join('\n  ')}`
    )
  }
  return found
}

function entrySegments(entry) {
  return entry.split('/').filter(Boolean)
}

/**
 * Real packages with versions, read from each module's package.json inside the
 * asar. Scope containers (@types, @vue, …) are not packages and are skipped;
 * anything else without a parseable package.json fails the build.
 */
export function collectModuleManifest(asarPath) {
  const entries = listPackage(asarPath)
  const names = new Set()
  const byName = new Map()
  for (const entry of entries) {
    const seg = entrySegments(entry)
    if (seg[0] !== 'node_modules') continue
    let moduleName = null
    if (seg.length === 2) {
      // '/node_modules/<pkg>' — directory of an unscoped package or a scope.
      if (seg[1].startsWith('@')) continue // scope container; children handled below
      moduleName = seg[1]
    } else if (seg.length === 3 && seg[1].startsWith('@')) {
      // '/node_modules/@scope/<pkg>' — directory of a scoped package.
      moduleName = `${seg[1]}/${seg[2]}`
    } else {
      continue
    }
    if (names.has(moduleName)) continue
    names.add(moduleName)

    const pkgEntry = `node_modules/${moduleName}/package.json`
    if (!entries.includes(`/${pkgEntry}`)) {
      throw new Error(`module ${moduleName} has no package.json inside ${asarPath}`)
    }
    let raw
    try {
      raw = extractFile(asarPath, pkgEntry).toString('utf-8')
    } catch (err) {
      throw new Error(`cannot read package.json of ${moduleName}: ${err.message}`)
    }
    let parsed
    try {
      parsed = JSON.parse(raw)
    } catch (err) {
      throw new Error(`corrupt package.json of ${moduleName}: ${err.message}`)
    }
    if (parsed.name !== moduleName || typeof parsed.version !== 'string') {
      throw new Error(
        `package.json of ${moduleName} inconsistent (name=${parsed.name}, version=${parsed.version})`
      )
    }
    byName.set(moduleName, `${moduleName}@${parsed.version}`)
  }
  return [...byName.values()].sort()
}

/** Contract violations: top-level server/, scripts/ and secret-like files. */
export function checkForbiddenPaths(asarPath) {
  const violations = []
  for (const entry of listPackage(asarPath)) {
    const seg = entrySegments(entry)
    if (seg.length === 0 || seg[0] === 'node_modules' || seg[0] === 'out') continue
    const basename = seg[seg.length - 1]
    if (SECRET_BASENAME_RE.test(basename)) {
      violations.push(`forbidden file ${entry}`)
      continue
    }
    if (FORBIDDEN_TOP_LEVEL.has(seg[0])) {
      violations.push(`forbidden top-level dir /${seg[0]}/`)
    }
  }
  return [...new Set(violations)]
}

function checkContract(asarPath) {
  const violations = checkForbiddenPaths(asarPath)
  if (violations.length > 0) {
    return `packaging contract violated in ${asarPath}:\n  ${violations.join('\n  ')}`
  }
  return null
}

function parseFlag(args, name) {
  const i = args.indexOf(name)
  return i === -1 ? undefined : args[i + 1]
}

/** CLI body; returns the process exit code (kept pure for tests). */
export function run(args) {
  const wantWrite = args.includes('--write')
  const wantCheck = args.includes('--check')
  const asarArg = parseFlag(args, '--asar')
  let asarPath
  try {
    asarPath = findAsars(asarArg)[0]
  } catch (err) {
    console.error(`asar-manifest: ${err.message}`)
    return 2
  }

  const contractError = checkContract(asarPath)
  if (contractError) {
    console.error(contractError)
    return 1
  }

  let manifest
  try {
    manifest = collectModuleManifest(asarPath)
  } catch (err) {
    console.error(`asar-manifest: ${err.message}`)
    return 1
  }

  if (!wantCheck && !wantWrite) {
    for (const line of manifest) console.log(line)
    return 0
  }

  if (wantWrite) {
    mkdirSync(dirname(baselineFile), { recursive: true })
    writeFileSync(baselineFile, `${manifest.join('\n')}\n`)
    console.log(`baseline written (${manifest.length} packages): ${baselineFile}`)
    return 0
  }

  // --check: baseline must exist; a missing baseline is a hard failure.
  if (!existsSync(baselineFile)) {
    console.error(
      `asar-manifest: baseline missing at ${baselineFile} — refusing to auto-create. ` +
        `Run "node scripts/asar-manifest.mjs --write" deliberately.`
    )
    return 1
  }
  const baseline = readFileSync(baselineFile, 'utf-8')
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .sort()
  const added = manifest.filter((m) => !baseline.includes(m))
  const removed = baseline.filter((m) => !manifest.includes(m))
  if (added.length === 0 && removed.length === 0) {
    console.log(`app.asar manifest unchanged (${manifest.length} packages)`)
    return 0
  }
  console.error('app.asar manifest drift:')
  for (const m of added) console.error(`  + ${m}`)
  for (const m of removed) console.error(`  - ${m}`)
  return 1
}

const invokedDirectly =
  process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href
if (invokedDirectly) {
  process.exitCode = run(process.argv.slice(2))
}
