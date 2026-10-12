import { useCallback, useEffect, useRef, useState } from 'react'

export const POLL_INITIAL_MS = 2000
export const POLL_MAX_MS = 10_000
const POLL_BACKOFF = 1.5

export interface PolledResource<T> {
  data: T | null
  error: Error | null
  loading: boolean
  reload: () => void
}

/** A `shouldPoll` for resources that are loaded once and never polled. */
export const neverPoll = () => false

/**
 * Loads a resource and keeps polling it while `shouldPoll(data)` is true.
 *
 * `load` and `shouldPoll` must be stable (useCallback or module constants); a new `load`
 * starts over. The interval starts at 2 s and backs off to at most 10 s, resetting on every
 * restart. Polling stops on unmount, when nothing is active, or after an error, and a
 * response that arrives after a newer request (or after unmount) is ignored.
 */
export function usePolledResource<T>(
  load: () => Promise<T>,
  shouldPoll: (data: T) => boolean,
): PolledResource<T> {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<Error | null>(null)
  const [loading, setLoading] = useState(true)
  const restartRef = useRef<() => void>(() => {})

  useEffect(() => {
    let cancelled = false
    let timer: number | undefined
    let delay = POLL_INITIAL_MS
    let sequence = 0

    async function tick() {
      const current = ++sequence
      try {
        const result = await load()
        if (cancelled || current !== sequence) return
        setData(result)
        setError(null)
        setLoading(false)
        if (shouldPoll(result)) {
          timer = window.setTimeout(() => void tick(), delay)
          delay = Math.min(delay * POLL_BACKOFF, POLL_MAX_MS)
        }
      } catch (caught) {
        if (cancelled || current !== sequence) return
        setError(caught instanceof Error ? caught : new Error(String(caught)))
        setLoading(false)
      }
    }

    function restart() {
      window.clearTimeout(timer)
      delay = POLL_INITIAL_MS
      void tick()
    }

    restartRef.current = restart
    restart()
    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [load, shouldPoll])

  const reload = useCallback(() => restartRef.current(), [])

  return { data, error, loading, reload }
}
