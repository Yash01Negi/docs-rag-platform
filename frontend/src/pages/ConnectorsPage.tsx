import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router'

import ConnectorForm from '../components/connectors/ConnectorForm'
import SyncStatusBadge from '../components/connectors/SyncStatusBadge'
import Pagination from '../components/documents/Pagination'
import { AlertIcon, ConnectorsIcon, RefreshIcon } from '../components/icons'
import { usePolledResource } from '../hooks/usePolledResource'
import {
  createConnector,
  fetchConnectorOptions,
  listConnectors,
  type Connector,
  type ConnectorOptions,
  type Page,
} from '../lib/api'
import { anySyncActive, hasConnectorError, intervalLabel } from '../lib/connectors'
import { formatDateTime } from '../lib/format'

const PAGE_SIZE = 20

const linkClass =
  'font-medium text-slate-900 hover:text-indigo-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 rounded-sm break-all'

function ErrorIndicator({ connector }: { connector: Connector }) {
  if (!hasConnectorError(connector) || !connector.last_error_message) return null
  return (
    <p
      className="mt-1 flex items-start gap-1 text-xs text-rose-700"
      title={connector.last_error_message}
    >
      <AlertIcon className="mt-0.5 h-3.5 w-3.5 shrink-0" />
      <span className="line-clamp-2">{connector.last_error_message}</span>
    </p>
  )
}

function ConnectorTable({ connectors }: { connectors: Connector[] }) {
  return (
    <>
      <div className="hidden overflow-x-auto md:block">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <caption className="sr-only">Connectors</caption>
          <thead className="bg-slate-50 text-left text-xs font-semibold tracking-wide text-slate-500 uppercase">
            <tr>
              <th scope="col" className="px-4 py-3">
                Name
              </th>
              <th scope="col" className="px-4 py-3">
                Source
              </th>
              <th scope="col" className="px-4 py-3">
                Schedule
              </th>
              <th scope="col" className="px-4 py-3">
                Status
              </th>
              <th scope="col" className="px-4 py-3">
                Last success
              </th>
              <th scope="col" className="px-4 py-3 text-right">
                Documents
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {connectors.map((connector) => (
              <tr key={connector.id} className="align-top hover:bg-slate-50">
                <td className="max-w-xs px-4 py-3">
                  <Link to={`/connectors/${connector.id}`} className={linkClass}>
                    {connector.name}
                  </Link>
                  <ErrorIndicator connector={connector} />
                </td>
                <td className="px-4 py-3">
                  <span className="mr-2 rounded bg-amber-50 px-1.5 py-0.5 text-[11px] font-semibold text-amber-800 ring-1 ring-amber-200 ring-inset">
                    S3
                  </span>
                  <span className="font-mono text-xs break-all text-slate-700">{connector.uri}</span>
                </td>
                <td className="px-4 py-3 text-slate-600">
                  {connector.enabled ? intervalLabel(connector.sync_interval_minutes) : 'Manual only'}
                </td>
                <td className="px-4 py-3">
                  <SyncStatusBadge status={connector.last_sync_status} />
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-slate-600">
                  {formatDateTime(connector.last_success_at)}
                </td>
                <td className="px-4 py-3 text-right text-slate-900 tabular-nums">
                  {connector.document_count}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="divide-y divide-slate-100 md:hidden">
        {connectors.map((connector) => (
          <li key={connector.id} className="px-4 py-3">
            <div className="flex items-start justify-between gap-3">
              <Link to={`/connectors/${connector.id}`} className={linkClass}>
                {connector.name}
              </Link>
              <SyncStatusBadge status={connector.last_sync_status} />
            </div>
            <p className="mt-1 font-mono text-xs break-all text-slate-600">{connector.uri}</p>
            <p className="mt-1 text-xs text-slate-500">
              {connector.document_count} documents ·{' '}
              {connector.enabled ? intervalLabel(connector.sync_interval_minutes) : 'Manual only'} ·
              last success {formatDateTime(connector.last_success_at)}
            </p>
            <ErrorIndicator connector={connector} />
          </li>
        ))}
      </ul>
    </>
  )
}

export default function ConnectorsPage() {
  const navigate = useNavigate()
  const [offset, setOffset] = useState(0)
  const [adding, setAdding] = useState(false)
  const [options, setOptions] = useState<ConnectorOptions | null>(null)
  const [optionsError, setOptionsError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    fetchConnectorOptions()
      .then((value) => !cancelled && setOptions(value))
      .catch(
        () => !cancelled && setOptionsError('Connector settings could not be loaded from the API.'),
      )
    return () => {
      cancelled = true
    }
  }, [])

  const load = useCallback(() => listConnectors({ limit: PAGE_SIZE, offset }), [offset])
  // Poll only while a sync is queued or running.
  const shouldPoll = useCallback((page: Page<Connector>) => anySyncActive(page.items), [])
  const connectors = usePolledResource(load, shouldPoll)
  const page = connectors.data

  return (
    <div className="space-y-6">
      <section className="flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between">
        <div>
          <h2 className="text-2xl font-semibold tracking-tight text-slate-900">Connectors</h2>
          <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
            Import PDF, Markdown and text files from Amazon S3. The backend worker syncs each
            connector on its schedule: new and changed objects are indexed, and documents whose
            objects were deleted leave search. Uploaded documents are never affected.
          </p>
        </div>
        {options?.configured && !adding && (
          <button
            type="button"
            onClick={() => setAdding(true)}
            className="shrink-0 rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600"
          >
            Add connector
          </button>
        )}
      </section>

      {optionsError && (
        <p role="alert" className="rounded-md bg-rose-50 p-3 text-sm text-rose-800">
          {optionsError}
        </p>
      )}

      {options && !options.configured && (
        <div className="flex gap-3 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm text-amber-900">
          <AlertIcon className="mt-0.5 h-5 w-5 shrink-0" />
          <div>
            <p className="font-medium">S3 connectors are not configured on the backend.</p>
            <p className="mt-1">
              Set <code className="font-mono">S3_ALLOWED_SOURCES</code> and the worker’s AWS
              credentials in the backend environment (see <code className="font-mono">docs/s3-setup.md</code>),
              then restart the API and worker. Credentials are never entered in the browser.
            </p>
          </div>
        </div>
      )}

      {adding && options && (
        <section
          aria-labelledby="new-connector-heading"
          className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
        >
          <h3 id="new-connector-heading" className="mb-4 text-sm font-semibold text-slate-900">
            New S3 connector
          </h3>
          <ConnectorForm
            mode="create"
            options={options}
            onCancel={() => setAdding(false)}
            onSubmit={async (values) => {
              const created = await createConnector(values)
              navigate(`/connectors/${created.id}`)
            }}
          />
        </section>
      )}

      <section
        aria-labelledby="connectors-heading"
        className="rounded-xl border border-slate-200 bg-white shadow-sm"
      >
        <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-4 py-3 sm:px-5">
          <h3 id="connectors-heading" className="text-sm font-semibold text-slate-900">
            Configured connectors
            {page && <span className="ml-2 font-normal text-slate-500">({page.total})</span>}
          </h3>
          <button
            type="button"
            onClick={connectors.reload}
            className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-indigo-600"
          >
            <RefreshIcon className="h-3.5 w-3.5" />
            Refresh
          </button>
        </div>

        {connectors.loading && !page && (
          <output className="block px-5 py-10 text-center text-sm text-slate-500">
            Loading connectors…
          </output>
        )}

        {connectors.error && (
          <div role="alert" className="m-4 rounded-md bg-rose-50 p-4 text-sm text-rose-800">
            <p className="font-medium">Connectors could not be loaded.</p>
            <p className="mt-1">{connectors.error.message}</p>
            <button
              type="button"
              onClick={connectors.reload}
              className="mt-2 font-medium underline focus-visible:outline-2 focus-visible:outline-rose-600"
            >
              Try again
            </button>
          </div>
        )}

        {page && page.items.length === 0 && !connectors.error && (
          <div className="px-5 py-12 text-center">
            <ConnectorsIcon className="mx-auto h-8 w-8 text-slate-300" />
            <p className="mt-2 text-sm font-medium text-slate-800">No connectors yet</p>
            <p className="mt-1 text-sm text-slate-500">
              Add an S3 bucket or folder to import its documents automatically.
            </p>
          </div>
        )}

        {page && page.items.length > 0 && <ConnectorTable connectors={page.items} />}

        {page && (
          <div className="border-t border-slate-200 px-4 py-3 empty:hidden sm:px-5">
            <Pagination
              label="Connector pages"
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
