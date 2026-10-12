import { Link, useOutletContext } from 'react-router'

import type { LayoutContext } from '../components/AppLayout'
import { CheckIcon, ClockIcon, RefreshIcon } from '../components/icons'
import { NAV_ITEMS } from '../components/navigation'
import StatusBadge from '../components/StatusBadge'

const IMPLEMENTED = [
  'FastAPI backend with health and readiness endpoints',
  'PostgreSQL with the pgvector extension, managed by Alembic migrations',
  'Environment-based configuration and structured JSON logging',
  'Application shell with live backend status',
  'Docker Compose development environment and CI checks',
]

const PLANNED = [
  'Document ingestion for Japanese and English sources',
  'Hybrid retrieval (vector + keyword) with reranking',
  'Grounded answers with citations',
  'Retrieval and answer-quality evaluation',
]

export default function DashboardPage() {
  const { backend } = useOutletContext<LayoutContext>()
  const database = backend.readiness?.checks.database

  const rows: { label: string; value: string }[] = [
    { label: 'API', value: backend.health ? `${backend.health.service} v${backend.health.version}` : '—' },
    { label: 'Environment', value: backend.health?.environment ?? '—' },
    {
      label: 'Database',
      value: database ? (database.status === 'ok' ? 'Ready' : (database.detail ?? 'Not ready')) : '—',
    },
    { label: 'pgvector', value: database?.pgvector_version ? `v${database.pgvector_version}` : '—' },
    { label: 'Last checked', value: backend.lastChecked?.toLocaleTimeString() ?? '—' },
  ]

  return (
    <div className="space-y-8">
      <section>
        <h2 className="text-2xl font-semibold tracking-tight text-slate-900">Welcome to YashRAG</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
          A bilingual (Japanese/English) document intelligence platform built on retrieval-augmented
          generation. This is the Phase 1 foundation: the platform services are running, and the
          document, chat and evaluation features will arrive in later milestones.
        </p>
      </section>

      <div className="grid gap-6 lg:grid-cols-5">
        <section
          aria-labelledby="system-status-heading"
          className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-2"
        >
          <div className="flex items-center justify-between gap-3 border-b border-slate-200 px-5 py-4">
            <h3 id="system-status-heading" className="text-sm font-semibold text-slate-900">
              System status
            </h3>
            <button
              type="button"
              onClick={backend.refresh}
              className="inline-flex items-center gap-1.5 rounded-md px-2 py-1 text-xs font-medium text-slate-600 hover:bg-slate-100 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-indigo-600"
            >
              <RefreshIcon className="h-3.5 w-3.5" />
              Refresh
            </button>
          </div>
          <div className="px-5 py-4">
            <StatusBadge state={backend.state} />
            <dl className="mt-4 divide-y divide-slate-100 text-sm">
              {rows.map((row) => (
                <div key={row.label} className="flex justify-between gap-4 py-2.5">
                  <dt className="text-slate-500">{row.label}</dt>
                  <dd className="text-right font-medium break-words text-slate-900">{row.value}</dd>
                </div>
              ))}
            </dl>
            {backend.state === 'offline' && (
              <p className="mt-3 rounded-md bg-rose-50 p-3 text-xs leading-5 text-rose-800">
                The frontend cannot reach the API. Start the backend with{' '}
                <code className="font-mono">docker compose up</code> and refresh.
              </p>
            )}
          </div>
        </section>

        <section
          aria-labelledby="progress-heading"
          className="rounded-xl border border-slate-200 bg-white shadow-sm lg:col-span-3"
        >
          <div className="border-b border-slate-200 px-5 py-4">
            <h3 id="progress-heading" className="text-sm font-semibold text-slate-900">
              Implementation status
            </h3>
          </div>
          <div className="grid gap-6 px-5 py-4 sm:grid-cols-2">
            <div>
              <h4 className="text-xs font-semibold tracking-wide text-emerald-700 uppercase">
                Implemented (Phase 1)
              </h4>
              <ul className="mt-3 space-y-2.5 text-sm text-slate-700">
                {IMPLEMENTED.map((item) => (
                  <li key={item} className="flex gap-2">
                    <CheckIcon className="mt-0.5 h-4 w-4 shrink-0 text-emerald-600" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div>
              <h4 className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
                Planned
              </h4>
              <ul className="mt-3 space-y-2.5 text-sm text-slate-600">
                {PLANNED.map((item) => (
                  <li key={item} className="flex gap-2">
                    <ClockIcon className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" />
                    {item}
                  </li>
                ))}
              </ul>
            </div>
          </div>
        </section>
      </div>

      <section aria-labelledby="modules-heading">
        <h3 id="modules-heading" className="text-sm font-semibold text-slate-900">
          Modules
        </h3>
        <div className="mt-3 grid gap-4 sm:grid-cols-3">
          {NAV_ITEMS.filter((item) => !item.available).map(({ to, label, icon: ItemIcon }) => (
            <Link
              key={to}
              to={to}
              className="group rounded-xl border border-slate-200 bg-white p-5 shadow-sm transition hover:border-indigo-200 hover:shadow focus-visible:outline-2 focus-visible:outline-indigo-600"
            >
              <div className="flex items-center gap-3">
                <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-slate-100 text-slate-600 group-hover:bg-indigo-50 group-hover:text-indigo-600">
                  <ItemIcon className="h-5 w-5" />
                </span>
                <span className="font-medium text-slate-900">{label}</span>
              </div>
              <p className="mt-3 text-xs font-medium text-amber-700">Coming in a later milestone</p>
            </Link>
          ))}
        </div>
      </section>
    </div>
  )
}
