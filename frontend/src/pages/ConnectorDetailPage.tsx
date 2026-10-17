import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router'

import ConnectorForm from '../components/connectors/ConnectorForm'
import SyncStatusBadge from '../components/connectors/SyncStatusBadge'
import ConfirmDialog from '../components/documents/ConfirmDialog'
import Pagination from '../components/documents/Pagination'
import { AlertIcon, ArrowLeftIcon, RefreshIcon, TrashIcon } from '../components/icons'
import { neverPoll, usePolledResource } from '../hooks/usePolledResource'
import {
  ApiError,
  cancelSyncRun,
  deleteConnector,
  fetchConnectorOptions,
  getConnector,
  getSyncRun,
  listSyncRuns,
  requestSync,
  updateConnector,
  type ConnectorDetail,
  type ConnectorOptions,
  type SyncRun,
} from '../lib/api'
import {
  intervalLabel,
  isSyncActive,
  runCounts,
  runDuration,
  shouldPollConnector,
} from '../lib/connectors'
import { formatDateTime, pluralize } from '../lib/format'

const RUN_PAGE_SIZE = 10

const actionClass =
  'inline-flex items-center gap-1.5 rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-50'

function message(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback
}

function Configuration({ connector }: { connector: ConnectorDetail }) {
  const rows: [string, string][] = [
    ['Provider', 'Amazon S3'],
    ['Bucket', connector.bucket],
    ['Prefix', connector.prefix || '(whole bucket)'],
    ['Schedule', connector.enabled ? intervalLabel(connector.sync_interval_minutes) : 'Manual only'],
    ['Next automatic sync', connector.enabled ? formatDateTime(connector.next_sync_at) : '—'],
    ['Last sync finished', formatDateTime(connector.last_sync_finished_at)],
    ['Last successful sync', formatDateTime(connector.last_success_at)],
    ['Created', formatDateTime(connector.created_at)],
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

function Counts({ run }: { run: SyncRun }) {
  return (
    <dl className="grid grid-cols-3 gap-3 sm:grid-cols-6">
      {runCounts(run).map(({ label, value }) => (
        <div key={label} className="rounded-lg bg-slate-50 px-3 py-2">
          <dt className="text-xs text-slate-500">{label}</dt>
          <dd
            className={`text-lg font-semibold tabular-nums ${
              label === 'Failed' && value > 0 ? 'text-rose-700' : 'text-slate-900'
            }`}
          >
            {value}
          </dd>
        </div>
      ))}
    </dl>
  )
}

function RunFailures({ connectorId, runId }: { connectorId: string; runId: string }) {
  const load = useCallback(() => getSyncRun(connectorId, runId), [connectorId, runId])
  const run = usePolledResource(load, neverPoll)
  if (run.error) {
    return <p className="text-xs text-rose-700">Details could not be loaded: {run.error.message}</p>
  }
  if (!run.data) return <output className="text-xs text-slate-500">Loading details…</output>
  if (run.data.failures.length === 0) {
    return <p className="text-xs text-slate-500">No objects failed or were skipped.</p>
  }
  return (
    <ul className="space-y-1.5 text-xs">
      {run.data.failures.map((failure) => (
        <li key={`${failure.kind}-${failure.key}`} className="rounded-md bg-slate-50 px-3 py-2">
          <span
            className={`mr-2 font-semibold ${failure.kind === 'failed' ? 'text-rose-700' : 'text-slate-600'}`}
          >
            {failure.kind === 'failed' ? 'Failed' : 'Skipped'}
          </span>
          <span className="font-mono break-all text-slate-800">{failure.key}</span>
          <p className="mt-0.5 text-slate-600">
            {failure.message} <span className="font-mono opacity-70">({failure.code})</span>
          </p>
        </li>
      ))}
    </ul>
  )
}

function SyncHistory({ connectorId }: { connectorId: string }) {
  const [offset, setOffset] = useState(0)
  const [open, setOpen] = useState<string | null>(null)
  const load = useCallback(
    () => listSyncRuns(connectorId, { limit: RUN_PAGE_SIZE, offset }),
    [connectorId, offset],
  )
  const runs = usePolledResource(load, neverPoll)
  const page = runs.data

  if (runs.error) {
    return (
      <p role="alert" className="px-5 py-6 text-sm text-rose-700">
        Sync history could not be loaded: {runs.error.message}
      </p>
    )
  }
  if (!page) return <output className="block px-5 py-6 text-sm text-slate-500">Loading history…</output>
  if (page.total === 0) {
    return <p className="px-5 py-6 text-sm text-slate-500">No syncs yet.</p>
  }
  return (
    <>
      <ol className="divide-y divide-slate-100">
        {page.items.map((run) => {
          const duration = runDuration(run)
          const expanded = open === run.id
          return (
            <li key={run.id} className="px-5 py-3">
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
                <SyncStatusBadge status={run.status} />
                <span className="text-slate-700">{formatDateTime(run.started_at ?? run.created_at)}</span>
                <span className="text-xs text-slate-500">
                  {run.trigger === 'manual' ? 'Manual' : 'Scheduled'}
                  {duration && ` · ${duration}`}
                  {run.attempts > 1 && ` · ${pluralize(run.attempts, 'attempt')}`}
                </span>
                <span className="text-xs text-slate-500">
                  {run.documents_imported} imported · {run.documents_updated} updated ·{' '}
                  {run.documents_unchanged} unchanged · {run.documents_removed} removed ·{' '}
                  {run.objects_skipped} skipped · {run.objects_failed} failed
                </span>
                <button
                  type="button"
                  aria-expanded={expanded}
                  onClick={() => setOpen(expanded ? null : run.id)}
                  className="ml-auto rounded-sm text-xs font-medium text-indigo-700 hover:underline focus-visible:outline-2 focus-visible:outline-indigo-600"
                >
                  {expanded ? 'Hide details' : 'Details'}
                </button>
              </div>
              {run.error_message && (
                <p className="mt-1 text-xs text-rose-700">
                  {run.error_message}{' '}
                  {run.error_code && <span className="font-mono opacity-80">({run.error_code})</span>}
                </p>
              )}
              {expanded && (
                <div className="mt-2">
                  <RunFailures connectorId={connectorId} runId={run.id} />
                </div>
              )}
            </li>
          )
        })}
      </ol>
      <div className="border-t border-slate-200 px-5 py-3 empty:hidden">
        <Pagination
          label="Sync history pages"
          total={page.total}
          limit={page.limit}
          offset={page.offset}
          onChange={setOffset}
        />
      </div>
    </>
  )
}

export default function ConnectorDetailPage() {
  const { connectorId = '' } = useParams()
  const navigate = useNavigate()
  const [options, setOptions] = useState<ConnectorOptions | null>(null)
  const [editing, setEditing] = useState(false)
  const [confirmingDelete, setConfirmingDelete] = useState(false)
  const [busy, setBusy] = useState<'sync' | 'toggle' | 'cancel' | 'delete' | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchConnectorOptions()
      .then((value) => !cancelled && setOptions(value))
      .catch(() => undefined) // only needed for editing; the page works without it
    return () => {
      cancelled = true
    }
  }, [])

  const load = useCallback(() => getConnector(connectorId), [connectorId])
  // Poll only while a sync is queued or running.
  const resource = usePolledResource(load, shouldPollConnector)
  const connector = resource.data

  async function act(kind: 'sync' | 'toggle' | 'cancel', action: () => Promise<unknown>) {
    setBusy(kind)
    setActionError(null)
    setNotice(null)
    try {
      await action()
      resource.reload()
    } catch (error) {
      setActionError(message(error, 'The request failed.'))
    } finally {
      setBusy(null)
    }
  }

  async function confirmDelete() {
    setBusy('delete')
    setActionError(null)
    try {
      await deleteConnector(connectorId)
      navigate('/connectors', { replace: true })
    } catch (error) {
      setActionError(message(error, 'The connector could not be deleted.'))
      setConfirmingDelete(false)
      setBusy(null)
    }
  }

  const backLink = (
    <Link
      to="/connectors"
      className="inline-flex items-center gap-1.5 rounded-sm text-sm font-medium text-slate-600 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600"
    >
      <ArrowLeftIcon className="h-4 w-4" />
      All connectors
    </Link>
  )

  if (resource.error && !connector) {
    const notFound = resource.error instanceof ApiError && resource.error.status === 404
    return (
      <div className="space-y-4">
        {backLink}
        <div role="alert" className="rounded-xl border border-rose-200 bg-rose-50 p-6 text-sm text-rose-800">
          <p className="font-medium">
            {notFound ? 'This connector does not exist.' : 'The connector could not be loaded.'}
          </p>
          {!notFound && <p className="mt-1">{resource.error.message}</p>}
        </div>
      </div>
    )
  }

  if (!connector) {
    return (
      <div className="space-y-4">
        {backLink}
        <output className="block text-sm text-slate-500">Loading connector…</output>
      </div>
    )
  }

  const active = connector.active_run
  const latest = connector.latest_run
  const current = active ?? latest
  const showError =
    connector.last_error_message !== null && !isSyncActive(connector.last_sync_status)

  return (
    <div className="space-y-6">
      {backLink}

      <section className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div className="min-w-0">
          <h2 className="text-xl font-semibold tracking-tight break-all text-slate-900">
            {connector.name}
          </h2>
          <p className="mt-1 font-mono text-sm break-all text-slate-600">{connector.uri}</p>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-sm text-slate-600">
            <SyncStatusBadge status={connector.last_sync_status} />
            <span>{pluralize(connector.document_count, 'document')}</span>
            {active?.cancel_requested && <span>Stopping after the current object…</span>}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {active ? (
            <button
              type="button"
              className={actionClass}
              disabled={busy !== null || active.cancel_requested}
              onClick={() => void act('cancel', () => cancelSyncRun(connector.id, active.id))}
            >
              {busy === 'cancel' ? 'Cancelling…' : 'Cancel sync'}
            </button>
          ) : (
            <button
              type="button"
              className={actionClass}
              disabled={busy !== null}
              onClick={() =>
                void act('sync', async () => {
                  await requestSync(connector.id)
                  setNotice('Sync queued. The worker starts it within a few seconds.')
                })
              }
            >
              <RefreshIcon className="h-4 w-4" />
              {busy === 'sync' ? 'Queuing…' : 'Sync now'}
            </button>
          )}
          <button
            type="button"
            className={actionClass}
            disabled={busy !== null}
            onClick={() =>
              void act('toggle', () => updateConnector(connector.id, { enabled: !connector.enabled }))
            }
          >
            {connector.enabled ? 'Pause automatic syncs' : 'Resume automatic syncs'}
          </button>
          <button
            type="button"
            className={actionClass}
            disabled={busy !== null || !options}
            onClick={() => setEditing(true)}
          >
            Edit
          </button>
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

      {notice && (
        <output className="block rounded-md bg-indigo-50 p-3 text-sm text-indigo-900">
          {notice}
        </output>
      )}
      {actionError && (
        <p role="alert" className="rounded-md bg-rose-50 p-3 text-sm text-rose-800">
          {actionError}
        </p>
      )}

      {showError && (
        <div
          role={connector.last_sync_status === 'partial' ? 'status' : 'alert'}
          className={`flex gap-3 rounded-xl border p-4 text-sm ${
            connector.last_sync_status === 'partial'
              ? 'border-amber-200 bg-amber-50 text-amber-900'
              : 'border-rose-200 bg-rose-50 text-rose-900'
          }`}
        >
          <AlertIcon className="mt-0.5 h-5 w-5 shrink-0" />
          <div>
            <p className="font-medium">
              {connector.last_sync_status === 'failed' ? 'The last sync failed' : 'Sync problem'}
            </p>
            <p className="mt-1">{connector.last_error_message}</p>
            {connector.last_error_code && (
              <p className="mt-1 font-mono text-xs opacity-80">code: {connector.last_error_code}</p>
            )}
            {connector.last_sync_status === 'failed' && (
              <p className="mt-1 text-xs">Nothing was deleted: removals only follow a complete listing.</p>
            )}
          </div>
        </div>
      )}

      {editing && options && (
        <section
          aria-labelledby="edit-connector-heading"
          className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
        >
          <h3 id="edit-connector-heading" className="mb-4 text-sm font-semibold text-slate-900">
            Edit connector
          </h3>
          <ConnectorForm
            mode="edit"
            options={options}
            initial={{
              name: connector.name,
              bucket: connector.bucket,
              prefix: connector.prefix,
              sync_interval_minutes: connector.sync_interval_minutes,
              enabled: connector.enabled,
            }}
            onCancel={() => setEditing(false)}
            onSubmit={async (values) => {
              await updateConnector(connector.id, {
                name: values.name,
                sync_interval_minutes: values.sync_interval_minutes,
                enabled: values.enabled,
              })
              setEditing(false)
              resource.reload()
            }}
          />
        </section>
      )}

      <div className="grid gap-6 lg:grid-cols-5">
        <section
          aria-labelledby="config-heading"
          className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-2"
        >
          <h3 id="config-heading" className="border-b border-slate-200 px-5 py-4 text-sm font-semibold text-slate-900">
            Configuration
          </h3>
          <div className="px-5 py-2">
            <Configuration connector={connector} />
          </div>
        </section>

        <section
          aria-labelledby="latest-heading"
          className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-3"
        >
          <h3 id="latest-heading" className="border-b border-slate-200 px-5 py-4 text-sm font-semibold text-slate-900">
            {active ? 'Current sync' : 'Last sync'}
          </h3>
          <div className="space-y-3 px-5 py-4">
            {current ? (
              <>
                <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
                  <SyncStatusBadge status={current.status} />
                  <span>{formatDateTime(current.started_at ?? current.created_at)}</span>
                  <span>· {current.objects_scanned} objects listed</span>
                </div>
                <Counts run={current} />
              </>
            ) : (
              <p className="text-sm text-slate-500">This connector has not synced yet.</p>
            )}
            <p className="text-xs text-slate-500">
              {pluralize(connector.failed_object_count, 'object')} currently rejected ·{' '}
              {pluralize(connector.deleted_object_count, 'object')} removed from the bucket
            </p>
          </div>
        </section>
      </div>

      <section
        aria-labelledby="history-heading"
        className="rounded-xl border border-slate-200 bg-white shadow-sm"
      >
        <h3 id="history-heading" className="border-b border-slate-200 px-5 py-4 text-sm font-semibold text-slate-900">
          Sync history
        </h3>
        {/* Remounted (and so reloaded) whenever the latest run or its status changes. */}
        <SyncHistory key={`${latest?.id ?? ''}:${latest?.status ?? ''}`} connectorId={connector.id} />
      </section>

      <ConfirmDialog
        open={confirmingDelete}
        title="Delete this connector?"
        confirmLabel="Delete connector and documents"
        busy={busy === 'delete'}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setConfirmingDelete(false)}
      >
        <p>
          This deletes <strong>{connector.name}</strong>, its sync history and the{' '}
          {pluralize(connector.document_count, 'document')} it imported; they leave search and chat
          at once. Uploaded documents are not affected, and nothing in the S3 bucket is changed.
        </p>
      </ConfirmDialog>
    </div>
  )
}
