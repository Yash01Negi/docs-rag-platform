import type {
  Connector,
  ConnectorDetail,
  ConnectorOptions,
  DocumentDetail,
  DocumentSummary,
  SyncRun,
  UploadLimits,
} from '../lib/api'

export const LIMITS: UploadLimits = {
  max_upload_bytes: 20 * 1024 * 1024,
  accepted_extensions: ['.pdf', '.md', '.markdown', '.txt'],
  accepted_mime_types: ['application/pdf', 'text/markdown', 'text/plain'],
}

export function makeDocument(overrides: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id: '7b0c9a7e-0000-4000-8000-000000000001',
    filename: 'report.pdf',
    file_type: 'pdf',
    mime_type: 'application/pdf',
    size_bytes: 123_456,
    language: 'en',
    status: 'completed',
    source_type: 'upload',
    chunk_count: 12,
    created_at: '2026-10-12T08:00:00Z',
    updated_at: '2026-10-12T08:01:00Z',
    error_code: null,
    error_message: null,
    ...overrides,
  }
}

export function makeDetail(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    ...makeDocument(),
    sha256: 'a'.repeat(64),
    page_count: 3,
    embedding_model: 'BAAI/bge-m3',
    embedding_model_revision: 'abc123',
    embedding_dimension: 1024,
    processed_at: '2026-10-12T08:01:00Z',
    latest_job: {
      id: 'job-1',
      kind: 'ingest',
      status: 'completed',
      attempts: 1,
      max_attempts: 3,
      created_at: '2026-10-12T08:00:00Z',
      started_at: '2026-10-12T08:00:05Z',
      completed_at: '2026-10-12T08:01:00Z',
      run_after: '2026-10-12T08:00:00Z',
      last_error_code: null,
      last_error_message: null,
    },
    source: null,
    ...overrides,
  }
}

export function file(name: string, size: number, type = 'application/octet-stream'): File {
  const result = new File(['x'], name, { type })
  Object.defineProperty(result, 'size', { value: size })
  return result
}

export const CONNECTOR_OPTIONS: ConnectorOptions = {
  configured: true,
  allowed_sources: ['team-docs', 'shared/policies/'],
  endpoint_override: false,
  supported_extensions: ['.pdf', '.md', '.markdown', '.txt'],
  max_object_bytes: 20 * 1024 * 1024,
  min_interval_minutes: 5,
  max_interval_minutes: 10080,
}

export function makeRun(overrides: Partial<SyncRun> = {}): SyncRun {
  return {
    id: 'run-1',
    connector_id: 'conn-1',
    trigger: 'manual',
    status: 'succeeded',
    attempts: 1,
    max_attempts: 3,
    cancel_requested: false,
    run_after: '2026-10-17T08:00:00Z',
    objects_scanned: 12,
    documents_imported: 3,
    documents_updated: 1,
    documents_unchanged: 6,
    documents_removed: 1,
    objects_skipped: 1,
    objects_failed: 0,
    error_code: null,
    error_message: null,
    created_at: '2026-10-17T08:00:00Z',
    started_at: '2026-10-17T08:00:01Z',
    finished_at: '2026-10-17T08:00:31Z',
    ...overrides,
  }
}

export function makeConnector(overrides: Partial<Connector> = {}): Connector {
  return {
    id: 'conn-1',
    name: 'Team handbook',
    provider: 's3',
    bucket: 'team-docs',
    prefix: 'handbook/',
    uri: 's3://team-docs/handbook/',
    enabled: true,
    sync_interval_minutes: 60,
    next_sync_at: '2026-10-17T09:00:31Z',
    last_sync_started_at: '2026-10-17T08:00:01Z',
    last_sync_finished_at: '2026-10-17T08:00:31Z',
    last_success_at: '2026-10-17T08:00:31Z',
    last_sync_status: 'succeeded',
    last_error_code: null,
    last_error_message: null,
    document_count: 10,
    created_at: '2026-10-16T08:00:00Z',
    updated_at: '2026-10-16T08:00:00Z',
    ...overrides,
  }
}

export function makeConnectorDetail(overrides: Partial<ConnectorDetail> = {}): ConnectorDetail {
  return {
    ...makeConnector(),
    failed_object_count: 0,
    deleted_object_count: 1,
    active_run: null,
    latest_run: makeRun(),
    ...overrides,
  }
}
