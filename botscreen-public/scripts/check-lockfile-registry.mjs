// Plain-JS CI helper (no TS type annotations).
/* eslint-disable @typescript-eslint/explicit-function-return-type */
// Lockfile source whitelist: every tarball source (resolved) in
// package-lock.json must come from the approved registry
// (https://registry.npmjs.org). Any other host — mirrors included — fails CI.
// Background: SCA-1B accidentally recorded 24 npmmirror sources because the
// dev machine's global npm registry points at npmmirror (since unified, the
// history baseline now also is fully official; see the SCA-2-prep PR).
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT_ENV = process.env.ASAR_MANIFEST_ROOT
const root = ROOT_ENV ? ROOT_ENV : join(dirname(fileURLToPath(import.meta.url)), '..')
const lockFile = join(root, 'package-lock.json')
const ALLOWED = 'registry.npmjs.org'

function main() {
  let lock
  try {
    lock = JSON.parse(readFileSync(lockFile, 'utf-8'))
  } catch (err) {
    console.error(`lockfile-registry: cannot read ${lockFile}: ${err.message}`)
    process.exitCode = 1
    return
  }
  const bad = []
  let total = 0
  for (const [name, pkg] of Object.entries(lock.packages ?? {})) {
    const resolved = pkg?.resolved
    if (!resolved) continue
    total += 1
    let host
    try {
      host = new URL(resolved).host
    } catch {
      bad.push(`${name}: unparsable resolved ${resolved}`)
      continue
    }
    if (host !== ALLOWED) bad.push(`${name}: ${resolved}`)
  }
  if (bad.length > 0) {
    console.error(`lockfile-registry: ${bad.length} non-approved source(s) (allowed: ${ALLOWED}):`)
    for (const line of bad.slice(0, 50)) console.error(`  ${line}`)
    process.exitCode = 1
    return
  }
  console.log(`lockfile-registry OK (${total} resolved sources all from ${ALLOWED})`)
}

main()
