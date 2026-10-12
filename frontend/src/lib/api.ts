// Minimal typed client for the backend API.

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const REQUEST_TIMEOUT_MS = 10_000
// Status checks fail fast so the header badge reacts quickly when the backend goes away.
const STATUS_TIMEOUT_MS = 5000

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

export type DocumentStatus = 'pending' | 'processing' | 'completed' | 'failed'
export type FileType = 'pdf' | 'markdown' | 'text'
export type Language = 'ja' | 'en' | 'mixed' | 'unknown'

export interface DocumentSummary {
  id: string
  filename: string
  file_type: FileType
  mime_type: string
  size_bytes: number
  language: Language | null
  status: DocumentStatus
  chunk_count: number
  created_at: string
  updated_at: string
  error_code: string | null
  error_message: string | null
}

export interface JobSummary {
  id: string
  kind: 'ingest' | 'reindex'
  status: DocumentStatus
  attempts: number
  max_attempts: number
  created_at: string
  started_at: string | null
  completed_at: string | null
  run_after: string
  last_error_code: string | null
  last_error_message: string | null
}

export interface DocumentDetail extends DocumentSummary {
  sha256: string
  page_count: number | null
  embedding_model: string | null
  embedding_model_revision: string | null
  embedding_dimension: number | null
  processed_at: string | null
  latest_job: JobSummary | null
}

export interface Page<T> {
  items: T[]
  total: number
  limit: number
  offset: number
}

export interface ChunkPreview {
  id: string
  ordinal: number
  text: string
  token_count: number
  language: Language
  page_start: number | null
  page_end: number | null
  section: string | null
}

export interface UploadLimits {
  max_upload_bytes: number
  accepted_extensions: string[]
  accepted_mime_types: string[]
}

export interface UploadResult {
  duplicate: boolean
  document: DocumentSummary
}

/** An error response from the API, with the backend's error code when available. */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId: string | null

  constructor(status: number, code: string, message: string, requestId: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; request_id?: string | null }
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: ErrorEnvelope = {}
  try {
    body = (await response.json()) as ErrorEnvelope
  } catch {
    // Not JSON (for example a proxy error page); fall back to the status line.
  }
  return new ApiError(
    response.status,
    body.error?.code ?? 'http_error',
    body.error?.message ?? `Request failed with HTTP ${response.status}`,
    body.error?.request_id ?? response.headers.get('X-Request-ID'),
  )
}

async function request<T>(
  path: string,
  init: RequestInit = {},
  acceptStatuses: number[] = [200],
): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
    signal: init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!acceptStatuses.includes(response.status)) {
    throw await toApiError(response)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function fetchHealth(): Promise<HealthResponse> {
  return request<HealthResponse>('/api/v1/health', {
    signal: AbortSignal.timeout(STATUS_TIMEOUT_MS),
  })
}

export function fetchReadiness(): Promise<ReadinessResponse> {
  // 503 is an expected answer ("not ready") with a meaningful body, not a transport error.
  return request<ReadinessResponse>(
    '/api/v1/ready',
    { signal: AbortSignal.timeout(STATUS_TIMEOUT_MS) },
    [200, 503],
  )
}

export function fetchUploadLimits(): Promise<UploadLimits> {
  return request<UploadLimits>('/api/v1/documents/limits')
}

export function listDocuments(params: {
  limit: number
  offset: number
  status?: DocumentStatus | null
}): Promise<Page<DocumentSummary>> {
  const query = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) })
  if (params.status) query.set('status', params.status)
  return request<Page<DocumentSummary>>(`/api/v1/documents?${query}`)
}

export function getDocument(id: string): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/api/v1/documents/${encodeURIComponent(id)}`)
}

export function listChunks(
  id: string,
  params: { limit: number; offset: number },
): Promise<Page<ChunkPreview>> {
  const query = new URLSearchParams({ limit: String(params.limit), offset: String(params.offset) })
  return request<Page<ChunkPreview>>(`/api/v1/documents/${encodeURIComponent(id)}/chunks?${query}`)
}

export function retryDocument(id: string): Promise<DocumentSummary> {
  return request<DocumentSummary>(
    `/api/v1/documents/${encodeURIComponent(id)}/retry`,
    { method: 'POST' },
    [202],
  )
}

export function reindexDocument(id: string): Promise<DocumentSummary> {
  return request<DocumentSummary>(
    `/api/v1/documents/${encodeURIComponent(id)}/reindex`,
    { method: 'POST' },
    [202],
  )
}

export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/api/v1/documents/${encodeURIComponent(id)}`, { method: 'DELETE' }, [204])
}

export function documentFileUrl(id: string, download = false): string {
  const suffix = download ? '?download=true' : ''
  return `${API_BASE_URL}/api/v1/documents/${encodeURIComponent(id)}/file${suffix}`
}

/**
 * Upload with XMLHttpRequest, because fetch() cannot report upload progress.
 * Resolves for 202 (new document) and 200 (duplicate content); rejects with ApiError otherwise.
 */
export function uploadDocument(
  file: File,
  onProgress?: (fraction: number) => void,
  signal?: AbortSignal,
): Promise<UploadResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', `${API_BASE_URL}/api/v1/documents`)
    xhr.setRequestHeader('Accept', 'application/json')
    xhr.responseType = 'json'
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable && onProgress) onProgress(event.loaded / event.total)
    }
    xhr.onload = () => {
      const body = xhr.response as (UploadResult & ErrorEnvelope) | null
      if (xhr.status === 200 || xhr.status === 202) {
        resolve(body as UploadResult)
        return
      }
      reject(
        new ApiError(
          xhr.status,
          body?.error?.code ?? 'http_error',
          body?.error?.message ?? `Upload failed with HTTP ${xhr.status}`,
          body?.error?.request_id ?? xhr.getResponseHeader('X-Request-ID'),
        ),
      )
    }
    xhr.onerror = () => reject(new ApiError(0, 'network_error', 'The server could not be reached.'))
    xhr.onabort = () => reject(new ApiError(0, 'aborted', 'The upload was cancelled.'))
    signal?.addEventListener('abort', () => xhr.abort())
    const form = new FormData()
    form.append('file', file)
    xhr.send(form)
  })
}

// --- Search (Phase 3) ---------------------------------------------------------------------

export type SearchMode = 'semantic' | 'keyword' | 'hybrid'
export type ScoreType = 'cosine_similarity' | 'bm25' | 'rrf'
export type SearchLanguage = 'ja' | 'en' | 'mixed'

// The first semantic search may have to load the embedding model, which takes a while.
const SEARCH_TIMEOUT_MS = 60_000

export interface SearchRequest {
  query: string
  /** Omit to use the server's default mode (SEARCH_DEFAULT_MODE). */
  mode?: SearchMode
  top_k?: number
  document_ids?: string[]
  language?: SearchLanguage
  min_similarity?: number
}

export interface MethodRank {
  rank: number
  score: number
}

export interface SearchResult {
  rank: number
  score: number
  chunk_id: string
  document_id: string
  filename: string
  file_type: FileType
  document_language: Language | null
  page_count: number | null
  chunk_ordinal: number
  language: Language
  page_start: number | null
  page_end: number | null
  section: string | null
  text: string
  snippet: string
  snippet_start: number
  highlights: [number, number][]
  retrieval_methods: ('semantic' | 'keyword')[]
  semantic: MethodRank | null
  keyword: MethodRank | null
}

export interface SearchResponse {
  query: string
  mode: SearchMode
  score_type: ScoreType
  score_description: string
  top_k: number
  results: SearchResult[]
  query_terms: string[]
  semantic_available: boolean
  warnings: string[]
  request_id: string | null
  timings_ms: Record<string, number>
}

export interface SearchOptions {
  modes: SearchMode[]
  default_mode: SearchMode
  default_top_k: number
  max_top_k: number
  max_query_chars: number
  rrf_k: number
  fusion_depth: number
}

export function searchDocuments(body: SearchRequest, signal?: AbortSignal): Promise<SearchResponse> {
  const timeout = AbortSignal.timeout(SEARCH_TIMEOUT_MS)
  return request<SearchResponse>('/api/v1/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  })
}

export function fetchSearchOptions(): Promise<SearchOptions> {
  return request<SearchOptions>('/api/v1/search/options')
}
