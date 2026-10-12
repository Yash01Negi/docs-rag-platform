import { useCallback, useEffect, useState } from 'react'

import DocumentTable from '../components/documents/DocumentTable'
import Pagination from '../components/documents/Pagination'
import UploadPanel from '../components/documents/UploadPanel'
import { DocumentsIcon, RefreshIcon } from '../components/icons'
import { usePolledResource } from '../hooks/usePolledResource'
import {
  fetchUploadLimits,
  listDocuments,
  type DocumentStatus,
  type DocumentSummary,
  type Page,
  type UploadLimits,
} from '../lib/api'
import { isActive, STATUS_LABELS } from '../lib/format'

const PAGE_SIZE = 20
const STATUS_FILTERS: (DocumentStatus | 'all')[] = [
  'all',
  'pending',
  'processing',
  'completed',
  'failed',
]

export default function DocumentsPage() {
  const [offset, setOffset] = useState(0)
  const [status, setStatus] = useState<DocumentStatus | 'all'>('all')
  const [limits, setLimits] = useState<UploadLimits | null>(null)
  const [limitsError, setLimitsError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchUploadLimits()
      .then((value) => !cancelled && setLimits(value))
      .catch(() => !cancelled && setLimitsError('Upload limits could not be loaded from the API.'))
    return () => {
      cancelled = true
    }
  }, [])

  const load = useCallback(
    () =>
      listDocuments({ limit: PAGE_SIZE, offset, status: status === 'all' ? null : status }),
    [offset, status],
  )
  const shouldPoll = useCallback(
    (page: Page<DocumentSummary>) => page.items.some((item) => isActive(item.status)),
    [],
  )
  const documents = usePolledResource(load, shouldPoll)
  const page = documents.data

  return (
    <div className="space-y-6">
      <section>
        <h2 className="text-2xl font-semibold tracking-tight text-slate-900">Documents</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
          Upload Japanese and English documents. Each upload is validated, split into chunks and
          embedded by the background ingestion worker. Indexed documents can be searched on the
          Search page; chatting over them arrives in a later milestone.
        </p>
      </section>

      <UploadPanel
        limits={limits}
        limitsError={limitsError}
        onUploaded={() => {
          setOffset(0)
          documents.reload()
        }}
      />

      <section
        aria-labelledby="library-heading"
        className="rounded-xl border border-slate-200 bg-white shadow-sm"
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-5">
          <h2 id="library-heading" className="text-sm font-semibold text-slate-900">
            Library
            {page && <span className="ml-2 font-normal text-slate-500">({page.total})</span>}
          </h2>
          <div className="flex items-center gap-2">
            <label htmlFor="status-filter" className="text-xs text-slate-600">
              Status
            </label>
            <select
              id="status-filter"
              value={status}
              onChange={(event) => {
                setStatus(event.target.value as DocumentStatus | 'all')
                setOffset(0)
              }}
              className="rounded-md border border-slate-300 bg-white px-2 py-1 text-sm focus-visible:outline-2 focus-visible:outline-indigo-600"
            >
              {STATUS_FILTERS.map((value) => (
                <option key={value} value={value}>
                  {value === 'all' ? 'All' : STATUS_LABELS[value]}
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={documents.reload}
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-indigo-600"
            >
              <RefreshIcon className="h-3.5 w-3.5" />
              Refresh
            </button>
          </div>
        </div>

        {documents.loading && !page && (
          <output className="block px-5 py-10 text-center text-sm text-slate-500">
            Loading documents…
          </output>
        )}

        {documents.error && (
          <div role="alert" className="m-4 rounded-md bg-rose-50 p-4 text-sm text-rose-800">
            <p className="font-medium">Documents could not be loaded.</p>
            <p className="mt-1">{documents.error.message}</p>
            <button
              type="button"
              onClick={documents.reload}
              className="mt-2 font-medium underline focus-visible:outline-2 focus-visible:outline-rose-600"
            >
              Try again
            </button>
          </div>
        )}

        {page && page.items.length === 0 && !documents.error && (
          <div className="px-5 py-12 text-center">
            <DocumentsIcon className="mx-auto h-8 w-8 text-slate-300" />
            <p className="mt-2 text-sm font-medium text-slate-800">
              {status === 'all' ? 'No documents yet' : `No ${STATUS_LABELS[status].toLowerCase()} documents`}
            </p>
            {status === 'all' && (
              <p className="mt-1 text-sm text-slate-500">Upload a PDF, Markdown or text file to begin.</p>
            )}
          </div>
        )}

        {page && page.items.length > 0 && <DocumentTable documents={page.items} />}

        {page && (
          <div className="border-t border-slate-200 px-4 py-3 empty:hidden sm:px-5">
            <Pagination
              label="Document pages"
              total={page.total}
              limit={page.limit}
              offset={page.offset}
              onChange={setOffset}
            />
          </div>
        )}
      </section>
    </div>
  )
}
