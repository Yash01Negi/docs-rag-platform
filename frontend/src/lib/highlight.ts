export interface Segment {
  text: string
  match: boolean
}

/**
 * Split text into plain and highlighted segments. Ranges are [start, end) character offsets
 * from the API; invalid or overlapping ranges are clamped or skipped rather than trusted.
 */
export function splitHighlights(text: string, ranges: [number, number][]): Segment[] {
  const segments: Segment[] = []
  let cursor = 0
  for (const [rawStart, rawEnd] of [...ranges].sort((a, b) => a[0] - b[0])) {
    const start = Math.max(cursor, Math.min(rawStart, text.length))
    const end = Math.min(Math.max(rawEnd, start), text.length)
    if (end <= start) continue
    if (start > cursor) segments.push({ text: text.slice(cursor, start), match: false })
    segments.push({ text: text.slice(start, end), match: true })
    cursor = end
  }
  if (cursor < text.length) segments.push({ text: text.slice(cursor), match: false })
  return segments
}
