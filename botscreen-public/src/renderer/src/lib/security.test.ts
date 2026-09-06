import { describe, expect, it } from 'vitest'
import { isSafeLocalVideoUrl, isSafeMarkdownExternalUrl, sanitizeMarkdownHtml } from './security'

describe('sanitizeMarkdownHtml', () => {
  it('removes javascript/data URIs', () => {
    const html = '<a href="javascript:alert(1)">x</a><img src="data:text/html,evil">'
    const out = sanitizeMarkdownHtml(html)
    expect(out).not.toContain('javascript:')
    expect(out).not.toContain('data:')
  })

  it('keeps rc:// media URIs', () => {
    const out = sanitizeMarkdownHtml('<a href="rc://videos/a.mp4">video</a>')
    expect(out).toContain('rc://videos/a.mp4')
  })
})

describe('isSafeLocalVideoUrl', () => {
  it('accepts rc video files', () => {
    expect(isSafeLocalVideoUrl('rc://media/a.mp4')).toBe(true)
    expect(isSafeLocalVideoUrl('rc://media/a.webm')).toBe(true)
  })

  it('rejects non-video, http, and unsafe rc', () => {
    expect(isSafeLocalVideoUrl('https://example.com/a.mp4')).toBe(false)
    expect(isSafeLocalVideoUrl('rc://media/a.html')).toBe(false)
    expect(isSafeLocalVideoUrl('rc://media/a.png')).toBe(false)
  })
})

describe('isSafeMarkdownExternalUrl', () => {
  it('accepts only rc local links in PR A', () => {
    expect(isSafeMarkdownExternalUrl('rc://media/a.mp4')).toBe(true)
    expect(isSafeMarkdownExternalUrl('https://example.com')).toBe(false)
    expect(isSafeMarkdownExternalUrl('mailto:a@b.com')).toBe(false)
  })
})
