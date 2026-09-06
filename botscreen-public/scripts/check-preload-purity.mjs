// CI gate: the compiled preload (out/preload/index.js) must stay loadable by a
// sandboxed renderer — sandboxed preloads can only require('electron'), so any
// third-party or node: module in the output would break preload loading.
// Run after `npm run build`.
import { existsSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = join(dirname(fileURLToPath(import.meta.url)), '..')
const preloadFile = join(root, 'out', 'preload', 'index.js')

if (!existsSync(preloadFile)) {
  console.error(`preload output missing: ${preloadFile} — run "npm run build" first`)
  process.exit(1)
}

const source = readFileSync(preloadFile, 'utf-8')
const requires = [...source.matchAll(/\brequire\(\s*["']([^"']+)["']\s*\)/g)].map((m) => m[1])
const forbidden = requires.filter((mod) => mod !== 'electron')

if (forbidden.length > 0) {
  console.error(
    `sandboxed preload must only require('electron'); found: ${[...new Set(forbidden)].join(', ')}`
  )
  process.exit(1)
}

console.log(`preload purity OK (external requires: ${requires.join(', ') || 'none'})`)
