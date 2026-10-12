import { describe, expect, it } from 'vitest'

import { pluralize } from './format'

describe('pluralize', () => {
  it('uses the singular only for exactly one', () => {
    expect(pluralize(1, 'chunk')).toBe('1 chunk')
    expect(pluralize(0, 'chunk')).toBe('0 chunks')
    expect(pluralize(12, 'indexed chunk')).toBe('12 indexed chunks')
  })
})
