import { useId } from 'react'

import type { ModelCatalog, ModelCost, ModelEntry } from '../../lib/chat'
import { COST_LABELS, modelKey } from '../../lib/modelSelection'
import { RefreshIcon } from '../icons'

const COST_STYLES: Record<ModelCost, string> = {
  paid: 'bg-violet-50 text-violet-800 ring-violet-200',
  free_tier: 'bg-sky-50 text-sky-800 ring-sky-200',
  local: 'bg-emerald-50 text-emerald-800 ring-emerald-200',
}

function optionLabel(model: ModelEntry): string {
  const parts = [model.model]
  if (model.default) parts.push('(default)')
  if (!model.configured) parts.push('— not configured')
  else if (model.availability.state === 'unavailable') parts.push('— unavailable')
  return parts.join(' ')
}

function availabilityText(model: ModelEntry): string {
  if (!model.configured) return 'Not configured'
  if (model.availability.state === 'available') return 'Available'
  if (model.availability.state === 'unavailable') return 'Unavailable'
  return 'Availability not confirmed'
}

interface Props {
  catalog: ModelCatalog
  selected: ModelEntry | null
  onSelect: (model: ModelEntry) => void
  onRefresh: () => void
  refreshing: boolean
  disabled: boolean
}

/** Provider and model picker for the next question, with cost class and availability. */
export default function ModelSelector({ catalog, selected, onSelect, onRefresh, refreshing, disabled }: Props) {
  const selectId = useId()
  const detailsId = useId()
  const provider = selected ? catalog.providers.find((p) => p.name === selected.provider) : undefined
  const unusable = catalog.models.filter((m) => !m.selectable)
  const anyUsable = unusable.length < catalog.models.length
  const reasons = new Map<string, string[]>()
  for (const model of unusable) {
    const reason = model.availability.reason ?? 'Unavailable.'
    reasons.set(reason, [...(reasons.get(reason) ?? []), `${model.provider_label}: ${model.model}`])
  }

  return (
    <div className="flex flex-col gap-1 text-xs text-slate-600">
      <div className="flex flex-wrap items-center gap-2">
        <label htmlFor={selectId} className="font-medium text-slate-700">
          Model
        </label>
        <select
          id={selectId}
          value={selected ? modelKey(selected) : ''}
          disabled={disabled}
          aria-describedby={detailsId}
          onChange={(event) => {
            const next = catalog.models.find((m) => modelKey(m) === event.target.value)
            if (next) onSelect(next)
          }}
          className="max-w-full min-w-0 rounded border border-slate-300 bg-white px-1.5 py-1 text-xs disabled:bg-slate-100"
        >
          {!selected && <option value="">No model available</option>}
          {catalog.providers.map((p) => {
            const models = catalog.models.filter((m) => m.provider === p.name)
            if (models.length === 0) return null
            return (
              <optgroup key={p.name} label={`${p.label} · ${COST_LABELS[p.cost]}`}>
                {models.map((m) => (
                  <option
                    key={modelKey(m)}
                    value={modelKey(m)}
                    // Keep an unusable choice visible when it is the selected one.
                    disabled={!m.selectable && modelKey(m) !== (selected ? modelKey(selected) : '')}
                  >
                    {optionLabel(m)}
                  </option>
                ))}
              </optgroup>
            )
          })}
        </select>
        <button
          type="button"
          onClick={onRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-1 rounded border border-slate-200 bg-white px-1.5 py-0.5 text-[11px] text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-indigo-600 disabled:opacity-50"
        >
          <RefreshIcon className="h-3 w-3" /> {refreshing ? 'Checking…' : 'Check again'}
        </button>
      </div>

      <div id={detailsId} className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {selected ? (
          <>
            <span className="text-slate-700">
              {selected.provider_label} · <span className="font-medium">{selected.model}</span>
            </span>
            <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ring-1 ${COST_STYLES[selected.cost]}`}>
              {COST_LABELS[selected.cost]}
            </span>
            <span
              className={
                selected.selectable
                  ? selected.availability.state === 'available'
                    ? 'text-emerald-700'
                    : 'text-slate-500'
                  : 'text-red-700'
              }
            >
              {availabilityText(selected)}
            </span>
          </>
        ) : (
          <span className="text-red-700">No configured model is available on the server.</span>
        )}
      </div>
      {!anyUsable && selected && (
        <p role="alert" className="rounded bg-red-50 px-2 py-1 text-red-800">
          No configured model is available on the server. See below for why.
        </p>
      )}
      {anyUsable && selected && !selected.selectable && (
        <p role="alert" className="rounded bg-red-50 px-2 py-1 text-red-800">
          {selected.model} cannot be used: {selected.availability.reason ?? 'unavailable'} Choose
          another model.
        </p>
      )}
      {selected?.selectable && selected.availability.state === 'unknown' && selected.availability.reason && (
        <p className="text-slate-500">{selected.availability.reason}</p>
      )}
      {provider?.notice && <p className="text-slate-500">{provider.notice}</p>}
      {unusable.length > 0 && (
        <details className="text-slate-500">
          <summary className="cursor-pointer select-none">
            Why {unusable.length === 1 ? 'one model is' : `${unusable.length} models are`} unavailable
          </summary>
          <ul className="mt-1 list-disc space-y-0.5 pl-5">
            {[...reasons.entries()].map(([reason, models]) => (
              <li key={reason}>
                <span className="text-slate-700">{models.join(', ')}</span>: {reason}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  )
}
