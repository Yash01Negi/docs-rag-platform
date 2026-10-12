import { Link } from 'react-router'

import { languageLabel, pageRange } from '../../lib/format'
import { sourceElementId, type SourceStatus } from '../../lib/chat'
import type { DisplaySource } from '../../lib/chatState'

const STATUS_NOTES: Record<SourceStatus, string | null> = {
  available: null,
  relocated: 'The document was reindexed; the link opens the same passage in the new index.',
  changed: 'The document has changed since this answer and no longer contains this passage.',
  deleted: 'The document has been deleted.',
}

function documentLink(source: DisplaySource): string | null {
  const status = source.source_status ?? 'available'
  if (status === 'deleted') return null
  if (status === 'changed') return `/documents/${source.document_id}`
  // A relocated passage has a new position; otherwise the stored one is still current.
  const ordinal = source.current_chunk_ordinal ?? source.chunk_ordinal
  return `/documents/${source.document_id}?chunk=${ordinal}`
}

interface Props {
  messageId: string
  sources: DisplaySource[]
  /** Source numbers the answer cites; null while the answer is still streaming. */
  cited: number[] | null
  expanded: Set<number>
  onToggle: (index: number) => void
}

export default function SourceList({ messageId, sources, cited, expanded, onToggle }: Props) {
  if (sources.length === 0) return null
  const isCited = (s: DisplaySource) => (cited ? cited.includes(s.index) : (s.supporting ?? false))
  const supporting = sources.filter(isCited)
  const related = sources.filter((s) => !isCited(s))
  return (
    <div className="mt-3 border-t border-slate-100 pt-3">
      {supporting.length > 0 && (
        <Group
          title={`Cited sources (${supporting.length})`}
          messageId={messageId}
          sources={supporting}
          expanded={expanded}
          onToggle={onToggle}
        />
      )}
      {related.length > 0 && (
        <details className="mt-2 group">
          <summary className="cursor-pointer text-xs font-medium text-slate-500 hover:text-slate-700">
            {cited === null ? 'Retrieved passages' : 'Related context, not cited'} ({related.length})
          </summary>
          <Group
            messageId={messageId}
            sources={related}
            expanded={expanded}
            onToggle={onToggle}
          />
        </details>
      )}
    </div>
  )
}

function Group({
  title,
  messageId,
  sources,
  expanded,
  onToggle,
}: {
  title?: string
  messageId: string
  sources: DisplaySource[]
  expanded: Set<number>
  onToggle: (index: number) => void
}) {
  return (
    <div>
      {title && <h4 className="text-xs font-semibold text-slate-600">{title}</h4>}
      <ol className="mt-1 space-y-1">
        {sources.map((source) => {
          const open = expanded.has(source.index)
          const pages = pageRange(source.page_start, source.page_end)
          const status = source.source_status ?? 'available'
          const link = documentLink(source)
          const detailsId = `${sourceElementId(messageId, source.index)}-details`
          return (
            <li
              key={source.index}
              id={sourceElementId(messageId, source.index)}
              tabIndex={-1}
              className="rounded-md border border-slate-200 bg-slate-50/60 focus-visible:outline-2 focus-visible:outline-indigo-600"
            >
              <button
                type="button"
                onClick={() => onToggle(source.index)}
                aria-expanded={open}
                aria-controls={detailsId}
                className="flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-indigo-600"
              >
                <span className="rounded bg-indigo-50 px-1.5 font-semibold text-indigo-700">
                  S{source.index}
                </span>
                <span className="min-w-0 flex-1 truncate font-medium text-slate-800" title={source.filename}>
                  {source.filename}
                </span>
                {pages && <span className="shrink-0 text-slate-500">{pages}</span>}
                {status !== 'available' && (
                  <span className="shrink-0 rounded bg-amber-50 px-1.5 text-amber-800">
                    {status === 'relocated' ? 'reindexed' : status}
                  </span>
                )}
                <span aria-hidden="true" className="shrink-0 text-slate-400">
                  {open ? '▾' : '▸'}
                </span>
              </button>
              {open && (
                <div id={detailsId} className="border-t border-slate-200 px-3 py-2 text-xs text-slate-700">
                  <p className="flex flex-wrap gap-x-3 gap-y-1 text-slate-500">
                    <span>Passage {source.chunk_ordinal + 1}</span>
                    <span>{languageLabel(source.language)}</span>
                    {source.section && <span>§ {source.section}</span>}
                    <span>Retrieval rank {source.retrieval_rank}</span>
                  </p>
                  {STATUS_NOTES[status] && (
                    <p className="mt-1 text-amber-800">{STATUS_NOTES[status]}</p>
                  )}
                  {source.excerpt ? (
                    <blockquote className="mt-2 max-h-48 overflow-y-auto border-l-2 border-slate-300 pl-2 whitespace-pre-wrap text-slate-700">
                      {source.excerpt}
                    </blockquote>
                  ) : (
                    <p className="mt-2 text-slate-500 italic">The passage text is no longer stored.</p>
                  )}
                  {link && (
                    <Link
                      to={link}
                      className="mt-2 inline-block font-medium text-indigo-700 hover:underline focus-visible:outline-2 focus-visible:outline-indigo-600"
                    >
                      {status === 'changed' ? 'Open the document' : 'Open this passage in the document'}
                    </Link>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ol>
    </div>
  )
}
