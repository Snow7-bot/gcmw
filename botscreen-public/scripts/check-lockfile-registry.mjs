// Plain-JS CI helper (no TS type annotations).
/* eslint-disable @typescript-eslint/explicit-function-return-type */
// Lockfile source whitelist (strict):
//  - every `resolved` must be https://registry.npmjs.org EXACTLY: official
//    protocol+host+default port, no credentials, no subdomains, no mirrors;
//  - every package that has a `resolved` must also carry a valid `integrity`;
//  - a missing/empty/malformed `packages` map fails the check;
//  - error output is sanitized: package path + reason only, NEVER the URL
//    (URLs can embed credentials or signed parameters).
//
// Usage: node scripts/check-lockfile-registry.mjs [--root <dir>]
// (root defaults to the package dir; tests override it to point at fixtures)
import { readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const DEFAULT_ROOT = join(SCRIPT_DIR, '..')
const ALLOWED_ORIGIN = 'https://registry.npmjs.org'
const INTEGRITY_RE = /^sha(?:1|256|384|512)-[A-Za-z0-9+/]+={0,2}$/

/** Structural issue about one lockfile entry; never embeds URL content. */
export function validateResolved(name, resolved, integrity) {
  let parsed
  try {
    parsed = new URL(resolved)
  } catch {
    return { name, reason: 'malformed resolved url' }
  }
  if (parsed.protocol !== 'https:') return { name, reason: 'non-https protocol' }
  if (parsed.username !== '' || parsed.password !== '') {
    return { name, reason: 'credentials embedded in url' }
  }
  if (parsed.origin !== ALLOWED_ORIGIN) return { name, reason: 'source origin not approved' }
  if (typeof integrity !== 'string' || !INTEGRITY_RE.test(integrity)) {
    return { name, reason: 'missing or invalid integrity' }
  }
  return null
}

/** Validate a parsed lockfile; returns sanitized issues (name + reason). */
export function checkLockfile(lock) {
  const issues = []
  if (lock === null || typeof lock !== 'object')
    return [{ name: '(lockfile)', reason: 'malformed lockfile json' }]
  const packages = lock.packages
  if (packages === null || typeof packages !== 'object')
    return [{ name: '(lockfile)', reason: 'missing packages map' }]
  const names = Object.keys(packages)
  if (names.length === 0) return [{ name: '(lockfile)', reason: 'empty packages map' }]
  for (const name of names) {
    const entry = packages[name]
    if (!entry || typeof entry !== 'object') continue
    if (entry.resolved !== undefined) {
      const issue = validateResolved(name, entry.resolved, entry.integrity)
      if (issue) issues.push(issue)
    }
  }
  return issues
}

/** CLI entry, returns exit code. */
export function run(args) {
  const rootFlag = args.indexOf('--root')
  const root = rootFlag !== -1 ? args[rootFlag + 1] : DEFAULT_ROOT
  const lockFile = join(root, 'package-lock.json')
  let text
  try {
    text = readFileSync(lockFile, 'utf-8')
  } catch {
    console.error('lockfile-registry: cannot read package-lock.json (missing?)')
    return 1
  }
  let lock
  try {
    lock = JSON.parse(text)
  } catch {
    console.error('lockfile-registry: package-lock.json is not valid JSON')
    return 1
  }
  const issues = checkLockfile(lock)
  if (issues.length > 0) {
    console.error(`lockfile-registry: ${issues.length} violation(s):`)
    for (const { name, reason } of issues) console.error(`  ${name}: ${reason}`)
    return 1
  }
  const total = Object.values(lock.packages ?? {}).filter((p) => p && p.resolved).length
  console.log(`lockfile-registry OK (${total} resolved sources, all official)`)
  return 0
}

const invokedDirectly =
  process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href
if (invokedDirectly) {
  process.exitCode = run(process.argv.slice(2))
}
