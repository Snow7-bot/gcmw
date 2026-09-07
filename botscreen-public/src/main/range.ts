/**
 * Pure HTTP Range resolution for the rc:// media protocol handler.
 * Unit-testable (no electron dependency).
 */

export type RangeStatus = 206 | 416

export interface ResolvedRange {
  /** 206 when a satisfiable byte range was resolved, 416 otherwise. */
  status: RangeStatus
  /** Inclusive start byte (valid when status === 206). */
  start: number
  /** Inclusive end byte, clamped to size - 1 (valid when status === 206). */
  end: number
  /** Number of bytes actually served: end - start + 1 (valid when status === 206). */
  length: number
}

/**
 * Resolve a `Range: bytes=start-end` header against a resource of `size` bytes.
 * - The header must match the single byte-range form EXACTLY (anchored
 *   full-string match): multi-range lists (`bytes=0-1,4-5`), embedded junk
 *   (`bytes=0-1junk`) or a garbage prefix (`xbytes=0-1`) are rejected.
 * - Suffix ranges (`bytes=-500`) are not supported in the first version and
 *   are rejected explicitly.
 * - Byte numbers must be non-negative safe integers; absurd values are
 *   rejected instead of silently wrapping.
 * - An explicit end beyond the file (e.g. `bytes=500-9999` on a 800-byte file)
 *   is clamped to `size - 1`; the caller must serve and declare the clamped
 *   range — never the unclamped end, otherwise Content-Range/Content-Length
 *   over-declare the response.
 */
const BYTE_RANGE_RE = /^bytes=(\d+)-(\d*)$/

export function resolveRange(rangeHeader: string | null, size: number): ResolvedRange {
  const match = BYTE_RANGE_RE.exec(rangeHeader ?? '')
  if (!match) {
    return { status: 416, start: 0, end: 0, length: 0 }
  }
  const start = Number(match[1])
  const end = match[2] === '' ? size - 1 : Number(match[2])
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || start < 0 || start >= size) {
    return { status: 416, start: 0, end: 0, length: 0 }
  }
  if (start > end) {
    return { status: 416, start: 0, end: 0, length: 0 }
  }
  const safeEnd = Math.min(end, size - 1)
  return { status: 206, start, end: safeEnd, length: safeEnd - start + 1 }
}
