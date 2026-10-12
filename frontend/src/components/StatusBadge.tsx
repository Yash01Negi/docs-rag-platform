import type { ConnectionState } from '../hooks/useBackendStatus'

const STYLES: Record<ConnectionState, { label: string; dot: string; badge: string }> = {
  checking: {
    label: 'Checking backend…',
    dot: 'bg-slate-400 animate-pulse',
    badge: 'bg-slate-100 text-slate-700 ring-slate-200',
  },
  online: {
    label: 'Backend connected',
    dot: 'bg-emerald-500',
    badge: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
  },
  degraded: {
    label: 'Backend up, database not ready',
    dot: 'bg-amber-500',
    badge: 'bg-amber-50 text-amber-800 ring-amber-200',
  },
  offline: {
    label: 'Backend unreachable',
    dot: 'bg-rose-500',
    badge: 'bg-rose-50 text-rose-800 ring-rose-200',
  },
}

export default function StatusBadge({ state }: { state: ConnectionState }) {
  const style = STYLES[state]
  return (
    <span
      role="status"
      aria-live="polite"
      className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-medium ring-1 ring-inset ${style.badge}`}
    >
      <span className={`h-2 w-2 rounded-full ${style.dot}`} aria-hidden="true" />
      <span className="hidden sm:inline">{style.label}</span>
      <span className="sm:hidden">{state === 'online' ? 'Connected' : style.label.split(',')[0]}</span>
    </span>
  )
}
