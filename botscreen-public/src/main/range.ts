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
 * - A missing/invalid range header resolves to 416 (caller keeps full-body path).
 * - An explicit end beyond the file (e.g. `bytes=500-9999` on a 800-byte file)
 *   is clamped to `size - 1`; the caller must serve and declare the clamped
 *   range — never the unclamped end, otherwise Content-Range/Content-Length
 *   over-declare the response.
 */
export function resolveRange(rangeHeader: string | null, size: number): ResolvedRange {
  const match = /bytes=(\d+)-(\d*)/.exec(rangeHeader ?? '')
  if (!match) {
    return { status: 416, start: 0, end: 0, length: 0 }
  }
  const start = Number(match[1])
  const end = match[2] ? Number(match[2]) : size - 1
  if (start >= size || start > end) {
    return { status: 416, start: 0, end: 0, length: 0 }
  }
  const safeEnd = Math.min(end, size - 1)
  return { status: 206, start, end: safeEnd, length: safeEnd - start + 1 }
}
