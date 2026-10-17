import { describe, expect, it } from 'vitest'

import { makeConnector, makeRun } from '../test/fixtures'
import {
  anySyncActive,
  hasConnectorError,
  intervalLabel,
  normalizePrefix,
  runCounts,
  runDuration,
  sourceAllowed,
  validateBucket,
  validateInterval,
  validateName,
  validatePrefix,
} from './connectors'

describe('connector validation', () => {
  it('accepts valid bucket names', () => {
    for (const bucket of ['team-docs', 'a.b-c', 'abc', ' docs ']) {
      expect(validateBucket(bucket)).toBeNull()
    }
  })

  it('rejects invalid bucket names with a reason', () => {
    expect(validateBucket('')).toMatch(/Enter a bucket/)
    expect(validateBucket('s3://team-docs')).toMatch(/only the bucket name/)
    expect(validateBucket('team-docs/handbook')).toMatch(/only the bucket name/)
    for (const bucket of ['ab', 'Team', 'team_docs', '-team', 'team-', 'x'.repeat(64)]) {
      expect(validateBucket(bucket)).toMatch(/3–63 lowercase/)
    }
    expect(validateBucket('a..b')).toMatch(/'\.\.'/)
    expect(validateBucket('10.0.0.1')).toMatch(/IP addresses/)
  })

  it('normalizes and validates prefixes', () => {
    expect(normalizePrefix('')).toBe('')
    expect(normalizePrefix(' handbook ')).toBe('handbook/')
    expect(normalizePrefix('a/b/')).toBe('a/b/')
    expect(validatePrefix('')).toBeNull()
    expect(validatePrefix('日本語/資料')).toBeNull()
    expect(validatePrefix('/abs')).toMatch(/must not start/)
    expect(validatePrefix('a\\b')).toMatch(/not allowed/)
    for (const prefix of ['a//b', 'a/../b', './a', '..']) {
      expect(validatePrefix(prefix)).toMatch(/empty, '\.' or '\.\.'/)
    }
    expect(validatePrefix('p'.repeat(600))).toMatch(/512/)
  })

  it('validates names and intervals', () => {
    expect(validateName('  ')).toMatch(/Enter a name/)
    expect(validateName('x'.repeat(101))).toMatch(/100/)
    expect(validateName('Handbook')).toBeNull()
    expect(validateInterval(4)).toMatch(/between 5 minutes and 7 days/)
    expect(validateInterval(10081)).not.toBeNull()
    expect(validateInterval(60)).toBeNull()
  })

  it('checks the server allowlist with folder-aligned prefixes', () => {
    const allowed = ['team-docs', 'shared/policies/']
    expect(sourceAllowed(allowed, 'team-docs', '')).toBe(true)
    expect(sourceAllowed(allowed, 'team-docs', 'any/folder')).toBe(true)
    expect(sourceAllowed(allowed, 'shared', 'policies')).toBe(true)
    expect(sourceAllowed(allowed, 'shared', 'policies/hr/')).toBe(true)
    expect(sourceAllowed(allowed, 'shared', '')).toBe(false)
    expect(sourceAllowed(allowed, 'shared', 'policiesx/')).toBe(false)
    expect(sourceAllowed(allowed, 'other', '')).toBe(false)
    expect(sourceAllowed([], 'team-docs', '')).toBe(false)
  })
})

describe('connector display helpers', () => {
  it('labels intervals', () => {
    expect(intervalLabel(60)).toBe('Every hour')
    expect(intervalLabel(1440)).toBe('Every day')
    expect(intervalLabel(120)).toBe('Every 2 hours')
    expect(intervalLabel(2880)).toBe('Every 2 days')
    expect(intervalLabel(45)).toBe('Every 45 minutes')
  })

  it('reports activity and errors', () => {
    expect(anySyncActive([makeConnector(), makeConnector({ last_sync_status: 'running' })])).toBe(
      true,
    )
    expect(anySyncActive([makeConnector({ last_sync_status: 'failed' })])).toBe(false)
    expect(hasConnectorError(makeConnector({ last_sync_status: 'failed' }))).toBe(true)
    expect(hasConnectorError(makeConnector({ last_sync_status: 'partial' }))).toBe(true)
    expect(hasConnectorError(makeConnector())).toBe(false)
    // A retry in progress is not shown as an error yet.
    expect(
      hasConnectorError(makeConnector({ last_sync_status: 'pending', last_error_message: 'x' })),
    ).toBe(false)
  })

  it('summarizes runs', () => {
    expect(runCounts(makeRun()).map((c) => c.value)).toEqual([3, 1, 6, 1, 1, 0])
    expect(runDuration(makeRun())).toBe('30 s')
    expect(runDuration(makeRun({ finished_at: '2026-10-17T08:02:06Z' }))).toBe('2 min 5 s')
    expect(runDuration(makeRun({ finished_at: null }))).toBeNull()
  })
})
