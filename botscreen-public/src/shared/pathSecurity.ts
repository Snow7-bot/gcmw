import fs from 'node:fs'
import path from 'node:path'

export const RC_ALLOWED_EXTENSIONS = [
  '.png',
  '.jpg',
  '.jpeg',
  '.webp',
  '.gif',
  '.mp4',
  '.webm',
  '.ogg',
  '.mov',
  '.m4v'
] as const

export const RC_MIME: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.gif': 'image/gif',
  '.mp4': 'video/mp4',
  '.webm': 'video/webm',
  '.ogg': 'video/ogg',
  '.mov': 'video/quicktime',
  '.m4v': 'video/x-m4v'
}

export function resolveAllowedPath(
  base: string,
  requestedPath: string,
  allowedExtensions: readonly string[]
): string | null {
  const resolved = path.isAbsolute(requestedPath)
    ? requestedPath
    : path.resolve(base, requestedPath)
  const rel = path.relative(base, resolved)
  if (rel === '..' || rel.startsWith(`..${path.sep}`) || path.isAbsolute(rel)) {
    return null
  }

  let realBase: string
  let realTarget: string
  try {
    realBase = fs.realpathSync(base)
    realTarget = fs.realpathSync(resolved)
  } catch {
    return null
  }
  const realRel = path.relative(realBase, realTarget)
  if (realRel === '..' || realRel.startsWith(`..${path.sep}`) || path.isAbsolute(realRel)) {
    return null
  }
  const ext = path.extname(realTarget).toLowerCase()
  if (!allowedExtensions.includes(ext)) {
    return null
  }
  return realTarget
}

export function mimeTypeForRcFile(filePath: string): string | null {
  const ext = path.extname(filePath).toLowerCase()
  return RC_MIME[ext] ?? null
}
