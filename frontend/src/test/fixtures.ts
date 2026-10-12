import type { DocumentDetail, DocumentSummary, UploadLimits } from '../lib/api'

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
    ...overrides,
  }
}

export function file(name: string, size: number, type = 'application/octet-stream'): File {
  const result = new File(['x'], name, { type })
  Object.defineProperty(result, 'size', { value: size })
  return result
}
