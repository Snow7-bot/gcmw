import createDOMPurify, { type UponSanitizeAttributeHook } from 'dompurify'

const ALLOWED_URI_REGEXP = /^(?:#|rc:\/\/)/i

const markdownPurifier = createDOMPurify(window)

const sanitizeUri: UponSanitizeAttributeHook = (_node, data) => {
  const attr = data.attrName.toLowerCase()
  if (attr !== 'href' && attr !== 'src') return

  const value = data.attrValue.trim().toLowerCase()
  const allowed =
    attr === 'href' ? value.startsWith('#') || value.startsWith('rc://') : value.startsWith('rc://')

  if (!allowed) {
    data.keepAttr = false
  }
}

markdownPurifier.addHook('uponSanitizeAttribute', sanitizeUri)

export function sanitizeMarkdownHtml(html: string): string {
  return markdownPurifier.sanitize(html, {
    ALLOWED_URI_REGEXP,
    FORBID_TAGS: [
      'style',
      'iframe',
      'object',
      'embed',
      'svg',
      'form',
      'input',
      'button',
      'textarea'
    ],
    FORBID_ATTR: ['style', 'srcset']
  })
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
