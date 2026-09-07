// Plain-JS CI helper (no TS type annotations).
/* eslint-disable @typescript-eslint/explicit-function-return-type */
// app.asar dependency manifest: lists every top-level node_modules package
// packaged inside the app.asar produced by `electron-builder --dir`.
//
// Usage:
//   node scripts/asar-manifest.mjs            # print manifest to stdout
//   node scripts/asar-manifest.mjs --check    # diff against packaging/app.asar.modules.txt
//
// The manifest is the SCA baseline: dependency-upgrade PRs must show app.asar
// contents only change deliberately (compare against this baseline).
import { createRequire } from 'node:module'
import { existsSync, readFileSync, writeFileSync, readdirSync, statSync, mkdirSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const { listPackage } = require('@electron/asar')

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const baselineFile = join(root, 'packaging', 'app.asar.modules.txt')

function findAsar() {
  const dist = join(root, 'dist')
  if (!existsSync(dist)) throw new Error(`dist/ missing — run "npm run build:unpack" first`)
  // Layouts: mac -> dist/mac*/<App>.app/Contents/Resources/app.asar,
  // win/linux -> dist/<target>-unpacked/resources/app.asar
  const queue = [dist]
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
      if (name === 'app.asar' && statSync(full).isFile()) return full
      let st
      try {
        st = statSync(full)
      } catch {
        continue
      }
      if (st.isDirectory() && name !== 'node_modules') queue.push(full)
    }
  }
  throw new Error('no app.asar found under dist/ — run "npm run build:unpack"')
}

function moduleNames(asarPath) {
  const files = listPackage(asarPath)
  const names = new Set()
  const prefix = '/node_modules/'
  for (const file of files) {
    if (!file.startsWith(prefix)) continue
    const parts = file.slice(prefix.length).split('/').filter(Boolean)
    if (parts.length === 0) continue
    const top = parts[0].startsWith('@') && parts.length > 1 ? `${parts[0]}/${parts[1]}` : parts[0]
    names.add(top)
  }
  return [...names].sort()
}

function main() {
  const check = process.argv.includes('--check')
  const manifest = moduleNames(findAsar())

  if (!check) {
    for (const line of manifest) console.log(line)
    return
  }

  if (!existsSync(baselineFile)) {
    mkdirSync(dirname(baselineFile), { recursive: true })
    writeFileSync(baselineFile, `${manifest.join('\n')}\n`)
    console.log(`baseline written: ${baselineFile}`)
    return
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
    return
  }
  console.error('app.asar manifest drift:')
  for (const m of added) console.error(`  + ${m}`)
  for (const m of removed) console.error(`  - ${m}`)
  process.exit(1)
}

main()
