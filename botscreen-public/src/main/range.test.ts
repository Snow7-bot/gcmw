import { describe, expect, it } from 'vitest'
import { resolveRange } from './range'

describe('resolveRange', () => {
  const SIZE = 800

  it('resolves an open-ended range to the last byte', () => {
    expect(resolveRange('bytes=500-', SIZE)).toEqual({
      status: 206,
      start: 500,
      end: 799,
      length: 300
    })
  })

  it('resolves an exact range within the file', () => {
    expect(resolveRange('bytes=100-199', SIZE)).toEqual({
      status: 206,
      start: 100,
      end: 199,
      length: 100
    })
  })

  it('clamps an explicit end beyond the file size', () => {
    // Previously the stream was clamped to 799 but Content-Range/Content-Length
    // declared 500-9999/800 — over-declaring the response.
    expect(resolveRange('bytes=500-9999', SIZE)).toEqual({
      status: 206,
      start: 500,
      end: 799,
      length: 300
    })
  })

  it('treats a start at the file boundary as satisfiable (empty-ish tail)', () => {
    expect(resolveRange('bytes=799-', SIZE)).toEqual({
      status: 206,
      start: 799,
      end: 799,
      length: 1
    })
  })

  it('rejects a start beyond the file', () => {
    expect(resolveRange('bytes=800-', SIZE).status).toBe(416)
  })

  it('rejects an inverted range', () => {
    expect(resolveRange('bytes=500-100', SIZE).status).toBe(416)
  })

  it('rejects malformed and unsupported range syntax (incl. suffix ranges)', () => {
    expect(resolveRange('bytes=-500', SIZE).status).toBe(416)
    expect(resolveRange(null, SIZE).status).toBe(416)
    expect(resolveRange('items=0-5', SIZE).status).toBe(416)
    expect(resolveRange('garbage', SIZE).status).toBe(416)
  })
})
