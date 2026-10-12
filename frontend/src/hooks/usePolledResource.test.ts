import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { POLL_INITIAL_MS, POLL_MAX_MS, usePolledResource } from './usePolledResource'

const active = (value: { active: boolean }) => value.active

describe('usePolledResource', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('polls while active with a bounded, growing interval and stops when done', async () => {
    let calls = 0
    const load = vi.fn(async () => ({ active: ++calls < 6 }))
    renderHook(() => usePolledResource(load, active))

    await act(async () => {})
    expect(load).toHaveBeenCalledTimes(1)

    const delays: number[] = []
    let delay = POLL_INITIAL_MS
    for (let i = 0; i < 5; i++) {
      delays.push(delay)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(delay)
      })
      delay = Math.min(delay * 1.5, POLL_MAX_MS)
    }
    expect(load).toHaveBeenCalledTimes(6)
    expect(Math.max(...delays)).toBeLessThanOrEqual(POLL_MAX_MS)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MAX_MS * 3)
    })
    expect(load).toHaveBeenCalledTimes(6) // inactive: polling stopped
  })

  it('stops polling on unmount', async () => {
    const load = vi.fn(async () => ({ active: true }))
    const { unmount } = renderHook(() => usePolledResource(load, active))
    await act(async () => {})
    unmount()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MAX_MS * 5)
    })
    expect(load).toHaveBeenCalledTimes(1)
  })

  it('reports errors and stops polling', async () => {
    const load = vi.fn(async () => {
      throw new Error('boom')
    })
    const { result } = renderHook(() => usePolledResource(load, active))
    await act(async () => {})
    expect(result.current.error?.message).toBe('boom')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(POLL_MAX_MS * 2)
    })
    expect(load).toHaveBeenCalledTimes(1)
  })
})
