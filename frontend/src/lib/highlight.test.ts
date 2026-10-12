import { describe, expect, it } from 'vitest'

import { splitHighlights } from './highlight'

describe('splitHighlights', () => {
  it('marks the given ranges', () => {
    expect(splitHighlights('埋め込みベクトルを保存', [[4, 8]])).toEqual([
      { text: '埋め込み', match: false },
      { text: 'ベクトル', match: true },
      { text: 'を保存', match: false },
    ])
  })

  it('returns the whole text when there are no ranges', () => {
    expect(splitHighlights('plain', [])).toEqual([{ text: 'plain', match: false }])
  })

  it('sorts, clamps and skips bad ranges', () => {
    expect(splitHighlights('abcdef', [[4, 99], [0, 1], [3, 2], [0, 2]])).toEqual([
      { text: 'a', match: true },
      { text: 'b', match: true },
      { text: 'cd', match: false },
      { text: 'ef', match: true },
    ])
  })
})
