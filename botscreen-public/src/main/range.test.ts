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

  it('rejects multi-range lists, embedded junk and prefix garbage (anchored match)', () => {
    // 全字符串匹配：不能只匹配到 "bytes=0-1" 前缀就放行
    expect(resolveRange('bytes=0-1,4-5', SIZE).status).toBe(416)
    expect(resolveRange('bytes=0-1,', SIZE).status).toBe(416)
    expect(resolveRange('bytes=0-1junk', SIZE).status).toBe(416)
    expect(resolveRange('xbytes=0-1', SIZE).status).toBe(416)
    expect(resolveRange('bytes=0-1 ', SIZE).status).toBe(416)
    expect(resolveRange(' bytes=0-1', SIZE).status).toBe(416)
  })

  it('rejects non-safe integers instead of silently wrapping', () => {
    const HUGE = '99999999999999999999'
    expect(resolveRange(`bytes=${HUGE}-`, SIZE).status).toBe(416)
    expect(resolveRange(`bytes=0-${HUGE}`, SIZE).status).toBe(416)
    expect(resolveRange('bytes=9007199254740992-', SIZE).status).toBe(416) // MAX_SAFE_INTEGER+1
    expect(resolveRange(`bytes=500-${HUGE}`, SIZE).status).toBe(416)
  })
})
