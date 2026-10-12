import type { DocumentStatus } from '../../lib/api'
import { STATUS_LABELS } from '../../lib/format'

const STYLES: Record<DocumentStatus, { dot: string; badge: string }> = {
  pending: { dot: 'bg-slate-400', badge: 'bg-slate-100 text-slate-700 ring-slate-200' },
  processing: {
    dot: 'bg-indigo-500 animate-pulse',
    badge: 'bg-indigo-50 text-indigo-800 ring-indigo-200',
  },
  completed: { dot: 'bg-emerald-500', badge: 'bg-emerald-50 text-emerald-800 ring-emerald-200' },
  failed: { dot: 'bg-rose-500', badge: 'bg-rose-50 text-rose-800 ring-rose-200' },
}

export default function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  const style = STYLES[status]
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium whitespace-nowrap ring-1 ring-inset ${style.badge}`}
    >
      <span className={`h-1.5 w-1.5 rounded-full ${style.dot}`} aria-hidden="true" />
      {STATUS_LABELS[status]}
    </span>
  )
}
