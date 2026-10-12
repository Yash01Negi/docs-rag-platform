import { useState } from 'react'

import { sourceElementId, type Grounding } from '../../lib/chat'
import type { AssistantView } from '../../lib/chatState'
import { AlertIcon, RefreshIcon } from '../icons'
import AnswerMarkdown from './AnswerMarkdown'
import SourceList from './SourceList'

const GROUNDING_NOTES: Partial<Record<Grounding, string>> = {
  insufficient_evidence: 'The documents do not contain enough information to answer this.',
  no_sources: 'No passage in your documents matched this question; no model was used.',
  uncited: 'This answer does not cite any passage. Check it against the documents.',
}

function formatSeconds(ms: number | null): string | null {
  return ms === null ? null : `${(ms / 1000).toFixed(1)} s`
}

interface Props {
  message: AssistantView
  onRetry?: () => void
  retryDisabled?: boolean
}

export default function AssistantMessage({ message, onRetry, retryDisabled }: Props) {
  const [expanded, setExpanded] = useState<Set<number>>(new Set())
  const known = new Set(message.sources.map((s) => s.index))

  function toggle(index: number) {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(index)) next.delete(index)
      else next.add(index)
      return next
    })
  }

  function showSource(index: number) {
    setExpanded((current) => new Set(current).add(index))
    // The related-context group may be collapsed; open it, then move focus to the source.
    requestAnimationFrame(() => {
      const element = document.getElementById(sourceElementId(message.id, index))
      element?.closest('details')?.setAttribute('open', '')
      element?.scrollIntoView?.({ block: 'nearest', behavior: 'smooth' })
      element?.focus()
    })
  }

  const busy = message.status === 'retrieving' || message.status === 'streaming'
  const failed = message.status === 'failed' || message.status === 'cancelled'
  const note = message.grounding ? GROUNDING_NOTES[message.grounding] : undefined
  const model = message.servedModel ?? message.model
  const fellBack = Boolean(message.servedModel && message.model && message.servedModel !== message.model)
  const meta = [
    model && (fellBack ? `Answered by ${model} (fallback from ${message.model})` : `Answered by ${model}`),
    formatSeconds(message.totalMs),
    message.usage ? `${message.usage.input_tokens + message.usage.output_tokens} tokens` : null,
  ].filter(Boolean)

  return (
    <article
      aria-label="Answer"
      aria-busy={busy}
      className="max-w-full rounded-2xl rounded-tl-sm border border-slate-200 bg-white px-4 py-3 shadow-sm sm:max-w-[85%]"
    >
      {message.status === 'retrieving' && (
        <p className="text-sm text-slate-500">Searching your documents…</p>
      )}
      {message.text && (
        <AnswerMarkdown text={message.text} onCite={showSource} knownSources={known} />
      )}
      {message.status === 'streaming' && (
        <span className="mt-1 inline-block h-4 w-1.5 animate-pulse bg-slate-400" aria-hidden="true" />
      )}
      {message.warnings.map((warning) => (
        <p key={warning} className="mt-2 text-xs text-amber-700">
          {warning}
        </p>
      ))}
      {note && <p className="mt-2 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800">{note}</p>}
      {message.stopReason === 'max_tokens' && (
        <p className="mt-2 text-xs text-amber-700">The answer reached the length limit and was cut off.</p>
      )}
      {failed && (
        <div
          role={message.status === 'failed' ? 'alert' : undefined}
          className="mt-2 flex flex-wrap items-center gap-2 rounded-md bg-red-50 px-2 py-1.5 text-xs text-red-800"
        >
          <AlertIcon className="h-4 w-4 shrink-0" />
          <span className="flex-1">
            {message.status === 'cancelled'
              ? 'Stopped before the answer finished.'
              : (message.error?.message ?? 'The answer could not be completed.')}
          </span>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              disabled={retryDisabled}
              className="inline-flex items-center gap-1 rounded border border-red-200 bg-white px-2 py-0.5 font-medium text-red-800 hover:bg-red-100 focus-visible:outline-2 focus-visible:outline-indigo-600 disabled:opacity-50"
            >
              <RefreshIcon className="h-3.5 w-3.5" /> Retry
            </button>
          )}
        </div>
      )}
      <SourceList
        messageId={message.id}
        sources={message.sources}
        cited={message.cited}
        expanded={expanded}
        onToggle={toggle}
      />
      {meta.length > 0 && !busy && (
        <p className="mt-2 text-[11px] text-slate-500">{meta.join(' · ')}</p>
      )}
    </article>
  )
}
