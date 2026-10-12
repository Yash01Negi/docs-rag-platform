import { Link } from 'react-router'

import type { ScoreType, SearchResult } from '../../lib/api'
import { FILE_TYPE_LABELS, languageLabel, pageRange } from '../../lib/format'
import { splitHighlights } from '../../lib/highlight'
import { SCORE_LABELS } from '../../lib/search'

const METHOD_LABELS = { semantic: 'Semantic', keyword: 'Keyword' } as const

function formatScore(type: ScoreType, score: number): string {
  // RRF scores are small sums of 1/(k + rank); show enough digits to tell them apart.
  return type === 'rrf' ? score.toFixed(4) : score.toFixed(3)
}

export function Highlighted({ text, ranges }: { text: string; ranges: [number, number][] }) {
  return (
    <>
      {splitHighlights(text, ranges).map((segment, index) =>
        segment.match ? (
          <mark key={index} className="rounded-sm bg-amber-100 px-0.5 text-slate-900">
            {segment.text}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      )}
    </>
  )
}

interface Props {
  result: SearchResult
  scoreType: ScoreType
}

export default function SearchResultCard({ result, scoreType }: Props) {
  const pages = pageRange(result.page_start, result.page_end)
  const snippetEnd = result.snippet_start + result.snippet.length
  const headingId = `result-${result.chunk_id}`
  return (
    <article
      aria-labelledby={headingId}
      className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5"
    >
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h3 id={headingId} className="text-sm font-semibold text-slate-900">
            <span className="mr-2 text-slate-400">#{result.rank}</span>
            <Link
              to={`/documents/${result.document_id}?chunk=${result.chunk_ordinal}`}
              className="break-all text-indigo-700 hover:underline focus-visible:outline-2 focus-visible:outline-indigo-600"
            >
              {result.filename}
            </Link>
          </h3>
          <p className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-500">
            <span>{FILE_TYPE_LABELS[result.file_type]}</span>
            {pages && <span>{pages}</span>}
            <span>Chunk {result.chunk_ordinal + 1}</span>
            <span>{languageLabel(result.language)}</span>
            {result.section && (
              <span className="max-w-full truncate" title={result.section}>
                § {result.section}
              </span>
            )}
          </p>
        </div>
        <div className="text-right text-xs">
          <p className="font-medium text-slate-700" title={`Score type: ${SCORE_LABELS[scoreType]}`}>
            {SCORE_LABELS[scoreType]} {formatScore(scoreType, result.score)}
          </p>
          <ul className="mt-1 flex justify-end gap-1" aria-label="Found by">
            {result.retrieval_methods.map((method) => {
              const detail = result[method]
              return (
                <li
                  key={method}
                  className={`rounded-full px-2 py-0.5 font-medium ${
                    method === 'semantic'
                      ? 'bg-violet-50 text-violet-700'
                      : 'bg-sky-50 text-sky-700'
                  }`}
                  title={
                    detail
                      ? `${METHOD_LABELS[method]} rank ${detail.rank}, ${
                          method === 'semantic' ? 'cosine similarity' : 'BM25'
                        } ${detail.score.toFixed(3)}`
                      : undefined
                  }
                >
                  {METHOD_LABELS[method]}
                  {detail && ` #${detail.rank}`}
                </li>
              )
            })}
          </ul>
        </div>
      </div>

      <p className="mt-3 text-sm leading-6 break-words whitespace-pre-line text-slate-800">
        {result.snippet_start > 0 && <span aria-hidden="true">… </span>}
        <Highlighted text={result.snippet} ranges={result.highlights} />
        {snippetEnd < result.text.length && <span aria-hidden="true"> …</span>}
      </p>

      {(result.snippet_start > 0 || snippetEnd < result.text.length) && (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer text-xs font-medium text-slate-600 hover:text-slate-900 focus-visible:outline-2 focus-visible:outline-indigo-600">
            Show the full chunk
          </summary>
          <p className="mt-2 rounded-md bg-slate-50 p-3 leading-6 break-words whitespace-pre-line text-slate-700">
            {result.text}
          </p>
        </details>
      )}
    </article>
  )
}
