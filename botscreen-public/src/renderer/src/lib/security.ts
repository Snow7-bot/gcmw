import DOMPurify from 'dompurify'

const ALLOWED_URI_REGEXP = /^(?:(?:https?|rc):|[^a-z]|[a-z+.-]+(?:[^a-z+.-:]|$))/i

export function sanitizeMarkdownHtml(html: string): string {
  const sanitized = DOMPurify.sanitize(html, { ALLOWED_URI_REGEXP })
  // Defense-in-depth: remove any remaining dangerous URI attributes.
  return sanitized.replace(/\s(?:href|src)=["'](?:javascript|data|file):[^"']*["']/gi, '')
}

const RC_VIDEO_EXTENSIONS = /\.(mp4|webm|ogg|mov|m4v)(\?.*)?$/i

export function isSafeLocalVideoUrl(url: string | null): url is string {
  if (!url) return false
  const trimmed = url.trim()
  if (!trimmed.toLowerCase().startsWith('rc://')) return false
  return RC_VIDEO_EXTENSIONS.test(trimmed)
}

export function isSafeMarkdownExternalUrl(url: string): boolean {
  const trimmed = url.trim().toLowerCase()
  // PR A disables external http/https links; they will be handled via safe IPC in PR B.
  return trimmed.startsWith('rc://')
}
