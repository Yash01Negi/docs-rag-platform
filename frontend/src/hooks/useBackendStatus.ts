import { useCallback, useEffect, useState } from 'react'

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
  const [refreshToken, setRefreshToken] = useState(0)

  const refresh = useCallback(() => setRefreshToken((token) => token + 1), [])

  useEffect(() => {
    let cancelled = false

    async function check() {
      const [healthResult, readinessResult] = await Promise.allSettled([
        fetchHealth(),
        fetchReadiness(),
      ])
      if (cancelled) return

      const nextHealth = healthResult.status === 'fulfilled' ? healthResult.value : null
      const nextReadiness = readinessResult.status === 'fulfilled' ? readinessResult.value : null

      setHealth(nextHealth)
      setReadiness(nextReadiness)
      setLastChecked(new Date())
      if (!nextHealth) setState('offline')
      else if (nextReadiness?.status === 'ready') setState('online')
      else setState('degraded')
    }

    void check()
    const interval = window.setInterval(() => void check(), POLL_INTERVAL_MS)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [refreshToken])

  return { state, health, readiness, lastChecked, refresh }
}
