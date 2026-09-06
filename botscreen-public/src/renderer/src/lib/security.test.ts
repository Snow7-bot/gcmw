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

describe('sanitizeMarkdownHtml expanded policy', () => {
  it('keeps #anchor in href only', () => {
    const out = sanitizeMarkdownHtml('<a href="#section">sec</a>')
    expect(out).toContain('href="#section"')
  })

  it('removes remote http/https and protocol-relative URLs', () => {
    const html =
      '<img src="https://example.com/a.png"><img src="http://example.com/b.png"><img src="//external/c.png">'
    const out = sanitizeMarkdownHtml(html)
    expect(out).not.toContain('https://')
    expect(out).not.toContain('http://')
    expect(out).not.toContain('//external')
  })

  it('removes javascript/data/file/vbscript', () => {
    const html =
      '<a href="javascript:alert(1)">j</a><a href="vbscript:x">v</a><img src="file:///etc/passwd">'
    const out = sanitizeMarkdownHtml(html)
    expect(out).not.toContain('javascript:')
    expect(out).not.toContain('vbscript:')
    expect(out).not.toContain('file:')
    expect(out).not.toContain('data:')
  })

  it('removes inline style, iframe, svg, form elements', () => {
    const html =
      '<p style="color:red">x</p><iframe src="https://x"></iframe><svg onload="alert(1)"></svg><form><input></form>'
    const out = sanitizeMarkdownHtml(html)
    expect(out).not.toContain('style=')
    expect(out).not.toContain('<iframe')
    expect(out).not.toContain('<svg')
    expect(out).not.toContain('<form')
    expect(out).not.toContain('<input')
  })

  it('is case-insensitive and handles surrounding whitespace', () => {
    const out = sanitizeMarkdownHtml(
      '<A HREF="  RC://Videos/A.MP4  ">x</A><a href="JaVaScRiPt:alert(1)">y</a>'
    )
    expect(out.toLowerCase()).toContain('rc://videos/a.mp4')
    expect(out.toLowerCase()).not.toContain('javascript:')
  })

  it('is repeatable without global hook pollution', () => {
    const first = sanitizeMarkdownHtml('<a href="rc://videos/a.mp4">x</a>')
    const second = sanitizeMarkdownHtml('<a href="javascript:alert(1)">y</a>')
    expect(first).toContain('rc://videos/a.mp4')
    expect(second).not.toContain('javascript:')
  })
})

describe('srcset and rc image XSS regression', () => {
  it('removes srcset even when mixed with rc and remote URLs', () => {
    const html = '<img src="rc://a.png" srcset="rc://a.png 1x, https://evil.example/x.png 2x">'
    const out = sanitizeMarkdownHtml(html)
    expect(out).not.toContain('srcset=')
    expect(out).not.toContain('evil.example')
    expect(out).toContain('rc://a.png')
  })

  it('removes onerror from rc images', () => {
    const html = '<img src="rc://a.png" onerror="alert(1)">'
    const out = sanitizeMarkdownHtml(html)
    expect(out).not.toContain('onerror=')
    expect(out).toContain('rc://a.png')
  })
})
