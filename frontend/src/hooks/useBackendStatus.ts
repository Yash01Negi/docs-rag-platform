import { useCallback, useEffect, useRef, useState } from 'react'

import { fetchHealth, fetchReadiness, type HealthResponse, type ReadinessResponse } from '../lib/api'

const POLL_INTERVAL_MS = 15_000

export type ConnectionState = 'checking' | 'online' | 'degraded' | 'offline'

export interface BackendStatus {
  state: ConnectionState
  health: HealthResponse | null
  readiness: ReadinessResponse | null
  lastChecked: Date | null
  refresh: () => void
}

/** Polls the backend liveness and readiness endpoints. */
export function useBackendStatus(): BackendStatus {
  const [state, setState] = useState<ConnectionState>('checking')
  const [health, setHealth] = useState<HealthResponse | null>(null)
  const [readiness, setReadiness] = useState<ReadinessResponse | null>(null)
  const [lastChecked, setLastChecked] = useState<Date | null>(null)
  // Incremented per check so a slow, older response cannot overwrite a newer one.
  const latestCheck = useRef(0)

  const check = useCallback(async () => {
    const checkId = ++latestCheck.current
    const [healthResult, readinessResult] = await Promise.allSettled([
      fetchHealth(),
      fetchReadiness(),
    ])
    if (checkId !== latestCheck.current) return

    const nextHealth = healthResult.status === 'fulfilled' ? healthResult.value : null
    const nextReadiness = readinessResult.status === 'fulfilled' ? readinessResult.value : null

    setHealth(nextHealth)
    setReadiness(nextReadiness)
    setLastChecked(new Date())
    if (!nextHealth) setState('offline')
    else if (nextReadiness?.status === 'ready') setState('online')
    else setState('degraded')
  }, [])

  useEffect(() => {
    // State is only set after the requests resolve, not synchronously in the effect.
    // oxlint-disable-next-line react/set-state-in-effect
    void check()
    const interval = window.setInterval(() => void check(), POLL_INTERVAL_MS)
    return () => {
      window.clearInterval(interval)
      latestCheck.current += 1 // ignore any check still in flight after unmount
    }
  }, [check])

  const refresh = useCallback(() => void check(), [check])

  return { state, health, readiness, lastChecked, refresh }
}
