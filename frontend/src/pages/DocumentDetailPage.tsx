import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router'

import ConfirmDialog from '../components/documents/ConfirmDialog'
import DocumentStatusBadge from '../components/documents/DocumentStatusBadge'
import Pagination from '../components/documents/Pagination'
import { AlertIcon, ArrowLeftIcon, DownloadIcon, RefreshIcon, TrashIcon } from '../components/icons'
import { neverPoll, usePolledResource } from '../hooks/usePolledResource'
import {
  ApiError,
  deleteDocument,
  documentFileUrl,
  getDocument,
  listChunks,
  reindexDocument,
  retryDocument,
  type DocumentDetail,
} from '../lib/api'
import {
  FILE_TYPE_LABELS,
  formatBytes,
  formatDateTime,
  isActive,
  languageLabel,
  pageRange,
  pluralize,
} from '../lib/format'

const CHUNK_PAGE_SIZE = 10

const actionClass =
  'inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-50'

// Bring the chunk a search result pointed at into view and move focus to it.
function scrollToChunk(node: HTMLLIElement | null) {
  node?.scrollIntoView?.({ block: 'center' })
  node?.focus()
}

function ChunkList({ documentId, focus }: { documentId: string; focus: number | null }) {
  // A search result links here with ?chunk=<ordinal>: open the page that contains it.
  const [offset, setOffset] = useState(
    focus === null ? 0 : Math.floor(focus / CHUNK_PAGE_SIZE) * CHUNK_PAGE_SIZE,
  )

  const load = useCallback(
    () => listChunks(documentId, { limit: CHUNK_PAGE_SIZE, offset }),
    [documentId, offset],
  )
  const chunks = usePolledResource(load, neverPoll)
  const page = chunks.data

  if (chunks.error) {
    return (
      <p role="alert" className="px-5 py-6 text-sm text-rose-700">
        Chunks could not be loaded: {chunks.error.message}
      </p>
    )
  }
  if (!page) {
    return (
      <output className="block px-5 py-6 text-sm text-slate-500">Loading chunks…</output>
    )
  }
  if (page.total === 0) {
    return <p className="px-5 py-6 text-sm text-slate-500">This document has no indexed chunks.</p>
  }
  return (
    <>
      <ol className="divide-y divide-slate-100">
        {page.items.map((chunk) => {
          const pages = pageRange(chunk.page_start, chunk.page_end)
          const isFocus = chunk.ordinal === focus
          return (
            <li
              key={chunk.id}
              ref={isFocus ? scrollToChunk : undefined}
              tabIndex={isFocus ? -1 : undefined}
              aria-current={isFocus ? 'true' : undefined}
              className={`px-5 py-4 ${isFocus ? 'bg-amber-50 outline-none ring-2 ring-amber-300 ring-inset' : ''}`}
            >
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                <span className="font-semibold text-slate-700">#{chunk.ordinal + 1}</span>
                {pages && <span>{pages}</span>}
                <span>{languageLabel(chunk.language)}</span>
                <span>{chunk.token_count} tokens</span>
                {chunk.section && (
                  <span className="truncate" title={chunk.section}>
                    § {chunk.section}
                  </span>
                )}
              </div>
              <p className="mt-2 line-clamp-6 text-sm leading-6 whitespace-pre-line text-slate-800">
                {chunk.text}
              </p>
            </li>
          )
        })}
      </ol>
      <div className="border-t border-slate-200 px-5 py-3 empty:hidden">
        <Pagination
          label="Chunk pages"
          total={page.total}
          limit={page.limit}
          offset={page.offset}
          onChange={setOffset}
        />
      </div>
    </>
  )
}

function Metadata({ document }: { document: DocumentDetail }) {
  const rows: [string, string][] = [
    ['File type', `${FILE_TYPE_LABELS[document.file_type]} (${document.mime_type})`],
    ['Size', formatBytes(document.size_bytes)],
    ['Pages', document.page_count?.toString() ?? '—'],
    ['Language', languageLabel(document.language)],
    ['Chunks', document.chunk_count.toString()],
    ['Uploaded', formatDateTime(document.created_at)],
    ['Indexed at', formatDateTime(document.processed_at)],
    [
      'Embedding model',
      document.embedding_model
        ? `${document.embedding_model} (${document.embedding_dimension ?? '?'} dimensions)`
        : '—',
    ],
    ['Model revision', document.embedding_model_revision ?? '—'],
    ['SHA-256', document.sha256],
  ]
  return (
    <dl className="divide-y divide-slate-100 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="grid grid-cols-3 gap-4 py-2.5">
          <dt className="text-slate-500">{label}</dt>
          <dd className="col-span-2 font-medium break-all text-slate-900">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

export default function DocumentDetailPage() {
  const { documentId = '' } = useParams()
  const [searchParams] = useSearchParams()
  const chunkParam = Number.parseInt(searchParams.get('chunk') ?? '', 10)
  const focusChunk = Number.isInteger(chunkParam) && chunkParam >= 0 ? chunkParam : null
  const navigate = useNavigate()
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState<'retry' | 'reindex' | 'delete' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const load = useCallback(() => getDocument(documentId), [documentId])
  const shouldPoll = useCallback((doc: DocumentDetail) => isActive(doc.status), [])
  const resource = usePolledResource(load, shouldPoll)
  const document = resource.data

  async function runAction(kind: 'retry' | 'reindex') {
    setBusy(kind)
    setActionError(null)
    try {
      await (kind === 'retry' ? retryDocument(documentId) : reindexDocument(documentId))
      resource.reload()
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : 'The request failed.')
    } finally {
      setBusy(null)
    }
  }

  async function confirmDelete() {
    setBusy('delete')
    setActionError(null)
    try {
      await deleteDocument(documentId)
      navigate('/documents', { replace: true })
    } catch (error) {
      setActionError(error instanceof ApiError ? error.message : 'The document could not be deleted.')
      setConfirmingDelete(false)
      setBusy(null)
    }
  }

  const backLink = (
    <Link
      to="/documents"
      className="inline-flex items-center gap-1.5 rounded-sm text-sm font-medium text-slate-600 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600"
    >
      <ArrowLeftIcon className="h-4 w-4" />
      All documents
    </Link>
  )

  if (resource.error && !document) {
    const notFound = resource.error instanceof ApiError && resource.error.status === 404
    return (
      <div className="space-y-4">
        {backLink}
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-800">
          <p className="font-medium">
            {notFound ? 'This document does not exist.' : 'The document could not be loaded.'}
          </p>
          {!notFound && <p className="mt-1">{resource.error.message}</p>}
        </div>
      </div>
    )
  }

  if (!document) {
    return (
      <div className="space-y-4">
        {backLink}
        <output className="block text-sm text-slate-500">Loading document…</output>
      </div>
    )
  }

  const active = isActive(document.status)
  const job = document.latest_job
  const reindexFailed = document.status === 'completed' && document.error_message

  return (
    <div className="space-y-6">
      {backLink}

      <section className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="text-xl font-semibold tracking-tight break-all text-slate-900">
            {document.filename}
          </h2>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-600">
            <DocumentStatusBadge status={document.status} />
            {active && document.chunk_count > 0 && (
              <span>Reindexing; the previous index stays active until the new one is ready.</span>
            )}
            {active && document.chunk_count === 0 && <span>Waiting for the ingestion worker.</span>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          <a href={documentFileUrl(document.id)} target="_blank" rel="noreferrer" className={actionClass}>
            Open original
          </a>
          <a href={documentFileUrl(document.id, true)} className={actionClass}>
            <DownloadIcon className="h-4 w-4" />
            Download
          </a>
          {document.status === 'failed' && (
            <button
              type="button"
              className={actionClass}
              onClick={() => void runAction('retry')}
              disabled={busy !== null}
            >
              <RefreshIcon className="h-4 w-4" />
              {busy === 'retry' ? 'Retrying…' : 'Retry ingestion'}
            </button>
          )}
          {document.status === 'completed' && (
            <button
              type="button"
              className={actionClass}
              onClick={() => void runAction('reindex')}
              disabled={busy !== null}
            >
              <RefreshIcon className="h-4 w-4" />
              {busy === 'reindex' ? 'Queuing…' : 'Reindex'}
            </button>
          )}
          <button
            type="button"
            className={`${actionClass} text-rose-700 hover:bg-rose-50`}
            onClick={() => setConfirmingDelete(true)}
            disabled={busy !== null}
          >
            <TrashIcon className="h-4 w-4" />
            Delete
          </button>
        </div>
      </section>

      {actionError && (
        <p role="alert" className="rounded-md bg-rose-50 p-3 text-sm text-rose-800">
          {actionError}
        </p>
      )}

      {document.error_message && (
        <div
          role={reindexFailed ? 'status' : 'alert'}
          className={`flex gap-3 rounded-xl border p-4 text-sm ${
            reindexFailed
              ? 'border-amber-200 bg-amber-50 text-amber-900'
              : 'border-rose-200 bg-rose-50 text-rose-900'
          }`}
        >
          <AlertIcon className="mt-0.5 h-5 w-5 shrink-0" />
          <div>
            <p className="font-medium">
              {reindexFailed ? 'The last reindex failed' : 'Ingestion failed'}
            </p>
            <p className="mt-1">{document.error_message}</p>
            {document.error_code && (
              <p className="mt-1 font-mono text-xs opacity-80">code: {document.error_code}</p>
            )}
          </div>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-5">
        <section
          aria-labelledby="metadata-heading"
          className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-3"
        >
          <h3 id="metadata-heading" className="border-b border-slate-200 px-5 py-4 text-sm font-semibold text-slate-900">
            Metadata
          </h3>
          <div className="px-5 py-2">
            <Metadata document={document} />
          </div>
        </section>

        <section
          aria-labelledby="job-heading"
          className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-2"
        >
          <h3 id="job-heading" className="border-b border-slate-200 px-5 py-4 text-sm font-semibold text-slate-900">
            Latest ingestion job
          </h3>
          {job ? (
            <dl className="divide-y divide-slate-100 px-5 py-2 text-sm">
              {(
                [
                  ['Type', job.kind === 'reindex' ? 'Reindex' : 'Ingest'],
                  ['Status', job.status],
                  ['Attempts', `${job.attempts} of ${job.max_attempts}`],
                  ['Queued', formatDateTime(job.created_at)],
                  ['Started', formatDateTime(job.started_at)],
                  ['Finished', formatDateTime(job.completed_at)],
                  ['Last error', job.last_error_message ?? '—'],
                ] as [string, string][]
              ).map(([label, value]) => (
                <div key={label} className="grid grid-cols-3 gap-4 py-2.5">
                  <dt className="text-slate-500">{label}</dt>
                  <dd className="col-span-2 font-medium break-words text-slate-900">{value}</dd>
                </div>
              ))}
            </dl>
          ) : (
            <p className="px-5 py-4 text-sm text-slate-500">No ingestion job recorded.</p>
          )}
        </section>
      </div>

      <section
        aria-labelledby="chunks-heading"
        className="rounded-xl border border-slate-200 bg-white shadow-sm"
      >
        <h3 id="chunks-heading" className="border-b border-slate-200 px-5 py-4 text-sm font-semibold text-slate-900">
          Chunks <span className="font-normal text-slate-500">({document.chunk_count})</span>
        </h3>
        {/* Remount when a new index is committed so the chunk list is refetched. */}
        <ChunkList
          key={document.processed_at ?? 'none'}
          documentId={document.id}
          focus={focusChunk}
        />
      </section>

      <ConfirmDialog
        open={confirmingDelete}
        title="Delete this document?"
        confirmLabel="Delete document"
        busy={busy === 'delete'}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setConfirmingDelete(false)}
      >
        <span className="font-medium break-all text-slate-900">{document.filename}</span>, its{' '}
        {pluralize(document.chunk_count, 'indexed chunk')} and the stored original file will be permanently
        deleted. This cannot be undone.
      </ConfirmDialog>
    </div>
  )
}
