import type { SyncRunStatus } from '../../lib/api'
import { SYNC_STATUS_LABELS } from '../../lib/connectors'

const STYLES: Record<SyncRunStatus | 'never', { dot: string; badge: string }> = {
  never: { dot: 'bg-slate-300', badge: 'bg-slate-50 text-slate-600 ring-slate-200' },
  pending: { dot: 'bg-slate-400', badge: 'bg-slate-100 text-slate-700 ring-slate-200' },
  running: {
    dot: 'bg-indigo-500 animate-pulse',
    badge: 'bg-indigo-50 text-indigo-800 ring-indigo-200',
  },
  succeeded: { dot: 'bg-emerald-500', badge: 'bg-emerald-50 text-emerald-800 ring-emerald-200' },
  partial: { dot: 'bg-amber-500', badge: 'bg-amber-50 text-amber-800 ring-amber-200' },
  failed: { dot: 'bg-rose-500', badge: 'bg-rose-50 text-rose-800 ring-rose-200' },
  cancelled: { dot: 'bg-slate-400', badge: 'bg-slate-100 text-slate-700 ring-slate-200' },
}

export default function SyncStatusBadge({ status }: { status: SyncRunStatus | null }) {
  const style = STYLES[status ?? 'never']
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset ${style.badge}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} aria-hidden="true" />
      {status ? SYNC_STATUS_LABELS[status] : 'Never synced'}
    </span>
  )
}
