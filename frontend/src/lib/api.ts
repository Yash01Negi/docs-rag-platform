// Minimal typed client for the backend health endpoints.

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const REQUEST_TIMEOUT_MS = 5000

export interface HealthResponse {
  status: 'ok'
  service: string
  version: string
  environment: string
}

export interface ReadinessResponse {
  status: 'ready' | 'not_ready'
  checks: {
    database: {
      status: 'ok' | 'error'
      pgvector_version?: string
      detail?: string
    }
  }
}

async function getJson<T>(path: string, acceptStatuses: number[] = [200]): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { Accept: 'application/json' },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!acceptStatuses.includes(response.status)) {
    throw new Error(`${path} returned HTTP ${response.status}`)
  }
  return (await response.json()) as T
}

export function fetchHealth(): Promise<HealthResponse> {
  return getJson<HealthResponse>('/api/v1/health')
}

export function fetchReadiness(): Promise<ReadinessResponse> {
  // 503 is an expected answer ("not ready") with a meaningful body, not a transport error.
  return getJson<ReadinessResponse>('/api/v1/ready', [200, 503])
}
