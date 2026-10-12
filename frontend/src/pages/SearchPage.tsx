import { useEffect, useId, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router'

import { AlertIcon, SearchIcon } from '../components/icons'
import SearchResultCard from '../components/search/SearchResultCard'
import {
  ApiError,
  listDocuments,
  fetchSearchOptions,
  searchDocuments,
  type DocumentSummary,
  type SearchLanguage,
  type SearchMode,
  type SearchResponse,
} from '../lib/api'
import { pluralize } from '../lib/format'
import { SCORE_LABELS } from '../lib/search'

const MODES: { value: SearchMode; label: string; description: string }[] = [
  {
    value: 'semantic',
    label: 'Semantic',
    description:
      'Finds passages with a similar meaning, also across Japanese and English. Scored best in our evaluation.',
  },
  {
    value: 'hybrid',
    label: 'Hybrid',
    description: 'Meaning and keywords combined (Reciprocal Rank Fusion).',
  },
  {
    value: 'keyword',
    label: 'Keyword',
    description: 'Matches words. Put words in "quotes" or 「」 to require an exact phrase.',
  },
]

const LANGUAGES: { value: SearchLanguage | ''; label: string }[] = [
  { value: '', label: 'Any language' },
  { value: 'ja', label: 'Japanese' },
  { value: 'en', label: 'English' },
  { value: 'mixed', label: 'Mixed (JA/EN)' },
]

const EXAMPLES = ['埋め込みベクトルの保存', 'How are documents chunked?', 'BGE-M3 モデル']

function isMode(value: string | null): value is SearchMode {
  return value === 'hybrid' || value === 'semantic' || value === 'keyword'
}

function isLanguage(value: string | null): value is SearchLanguage {
  return value === 'ja' || value === 'en' || value === 'mixed'
}

type SearchState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; error: ApiError | Error }
  | { status: 'done'; response: SearchResponse }

interface Settled {
  key: string
  response?: SearchResponse
  error?: Error
}

function requestKey(...parts: (string | number)[]): string {
  return JSON.stringify(parts)
}

export default function SearchPage() {
  const [params, setParams] = useSearchParams()
  const query = params.get('q')?.trim() ?? ''
  // Without ?mode= the server's default applies; the selector shows it once known.
  const urlMode = isMode(params.get('mode')) ? (params.get('mode') as SearchMode) : null
  const [defaultMode, setDefaultMode] = useState<SearchMode>('semantic')
  const mode: SearchMode = urlMode ?? defaultMode
  const language = isLanguage(params.get('lang')) ? (params.get('lang') as SearchLanguage) : ''
  const documentId = params.get('doc') ?? ''

  const [draft, setDraft] = useState(query)
  const [draftQuery, setDraftQuery] = useState(query)
  const [settled, setSettled] = useState<Settled | null>(null)
  const [documents, setDocuments] = useState<DocumentSummary[] | null>(null)
  const [retry, setRetry] = useState(0)
  const inputId = useId()

  // Keep the input in sync when the URL changes (back/forward navigation).
  if (draftQuery !== query) {
    setDraftQuery(query)
    setDraft(query)
  }

  const currentKey = requestKey(query, urlMode ?? '', language, documentId, retry)
  let state: SearchState
  if (!query) state = { status: 'idle' }
  else if (settled?.key !== currentKey) state = { status: 'loading' }
  else if (settled.error) state = { status: 'error', error: settled.error }
  else state = { status: 'done', response: settled.response as SearchResponse }

  useEffect(() => {
    let cancelled = false
    fetchSearchOptions()
      .then((options) => !cancelled && setDefaultMode(options.default_mode))
      .catch(() => {})
    listDocuments({ limit: 100, offset: 0, status: 'completed' })
      .then((page) => !cancelled && setDocuments(page.items))
      .catch(() => !cancelled && setDocuments([]))
    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    if (!query) return
    const key = requestKey(query, urlMode ?? '', language, documentId, retry)
    const controller = new AbortController()
    searchDocuments(
      {
        query,
        ...(urlMode ? { mode: urlMode } : {}),
        ...(language ? { language } : {}),
        ...(documentId ? { document_ids: [documentId] } : {}),
      },
      controller.signal,
    )
      .then((response) => setSettled({ key, response }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setSettled({ key, error: error instanceof Error ? error : new Error(String(error)) })
      })
    return () => controller.abort()
  }, [query, urlMode, language, documentId, retry])

  function update(changes: Record<string, string>) {
    const next = new URLSearchParams(params)
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value)
      else next.delete(key)
    }
    setParams(next)
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault()
    const trimmed = draft.trim()
    if (trimmed === query) setRetry((n) => n + 1)
    update({ q: trimmed })
  }

  // The summary names the mode the server actually used.
  const resultMode = state.status === 'done' ? state.response.mode : mode
  const activeMode = MODES.find((item) => item.value === resultMode) ?? MODES[0]

  return (
    <div className="space-y-6">
      <section>
        <h2 className="text-2xl font-semibold tracking-tight text-slate-900">Search</h2>
        <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-600">
          Search your indexed documents in Japanese, English or both. Results are passages from
          the documents themselves, ranked by relevance; nothing is generated or summarised.
        </p>
      </section>

      <search>
        <form
          onSubmit={onSubmit}
          aria-label="Search documents"
          className="space-y-4 rounded-xl border border-slate-200 bg-white p-4 shadow-sm sm:p-5"
        >
          <div>
            <label htmlFor={inputId} className="sr-only">
              Search query
            </label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <div className="relative flex-1">
                <SearchIcon className="pointer-events-none absolute top-1/2 left-3 h-5 w-5 -translate-y-1/2 text-slate-400" />
                <input
                  id={inputId}
                  type="search"
                  value={draft}
                  onChange={(event) => setDraft(event.target.value)}
                  placeholder="例: 埋め込みベクトル / How is text chunked?"
                  maxLength={1000}
                  autoComplete="off"
                  className="w-full rounded-lg border border-slate-300 py-3 pr-3 pl-10 text-base text-slate-900 placeholder:text-slate-400 focus-visible:border-indigo-500 focus-visible:outline-2 focus-visible:outline-indigo-600"
                />
              </div>
              <button
                type="submit"
                disabled={!draft.trim()}
                className="rounded-lg bg-indigo-600 px-5 py-3 text-sm font-semibold text-white hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Search
              </button>
            </div>
          </div>

          <fieldset>
            <legend className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
              Search mode
            </legend>
            <div className="mt-2 grid gap-2 sm:grid-cols-3">
              {MODES.map((item) => (
                <div
                  key={item.value}
                  className={`relative flex gap-2 rounded-lg border p-3 text-sm has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-indigo-600 ${
                    item.value === mode
                      ? 'border-indigo-500 bg-indigo-50'
                      : 'border-slate-200 hover:border-slate-300'
                  }`}
                >
                  <input
                    id={`mode-${item.value}`}
                    type="radio"
                    name="mode"
                    value={item.value}
                    checked={item.value === mode}
                    onChange={() => update({ mode: item.value === defaultMode ? '' : item.value })}
                    aria-describedby={`mode-${item.value}-description`}
                    className="mt-0.5 accent-indigo-600"
                  />
                  <div>
                    {/* The label covers the whole card so any click selects the mode. */}
                    <label
                      htmlFor={`mode-${item.value}`}
                      className="block cursor-pointer font-medium text-slate-900 after:absolute after:inset-0"
                    >
                      {item.label}
                    </label>
                    <p id={`mode-${item.value}-description`} className="text-xs leading-5 text-slate-600">
                      {item.description}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </fieldset>

          <div className="flex flex-col gap-3 sm:flex-row">
            <div className="flex-1">
              <label htmlFor="search-document" className="text-xs font-medium text-slate-600">
                Document
              </label>
              <select
                id="search-document"
                value={documentId}
                onChange={(event) => update({ doc: event.target.value })}
                className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm focus-visible:outline-2 focus-visible:outline-indigo-600"
              >
                <option value="">All indexed documents</option>
                {documents?.map((document) => (
                  <option key={document.id} value={document.id}>
                    {document.filename}
                  </option>
                ))}
              </select>
            </div>
            <div className="sm:w-48">
              <label htmlFor="search-language" className="text-xs font-medium text-slate-600">
                Passage language
              </label>
              <select
                id="search-language"
                value={language}
                onChange={(event) => update({ lang: event.target.value })}
                className="mt-1 w-full rounded-md border border-slate-300 bg-white px-2 py-2 text-sm focus-visible:outline-2 focus-visible:outline-indigo-600"
              >
                {LANGUAGES.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
        </form>
      </search>

      <section aria-labelledby="results-heading" aria-busy={state.status === 'loading'}>
        <h2 id="results-heading" className="sr-only">
          Results
        </h2>

        {state.status === 'idle' && (
          <div className="rounded-xl border border-dashed border-slate-300 px-5 py-10 text-center">
            <SearchIcon className="mx-auto h-8 w-8 text-slate-300" />
            <p className="mt-2 text-sm font-medium text-slate-800">Search your documents</p>
            <p className="mt-1 text-sm text-slate-500">Try one of these:</p>
            <div className="mt-3 flex flex-wrap justify-center gap-2">
              {EXAMPLES.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => update({ q: example })}
                  className="rounded-full border border-slate-200 bg-white px-3 py-1 text-sm text-slate-700 hover:border-indigo-300 hover:text-indigo-700 focus-visible:outline-2 focus-visible:outline-indigo-600"
                >
                  {example}
                </button>
              ))}
            </div>
          </div>
        )}

        {state.status === 'loading' && (
          <output className="block py-10 text-center text-sm text-slate-500">
            Searching… The first semantic search can take a while if the model is still loading.
          </output>
        )}

        {state.status === 'error' && (
          <div role="alert" className="rounded-md bg-rose-50 p-4 text-sm text-rose-800">
            <p className="flex items-center gap-2 font-medium">
              <AlertIcon className="h-4 w-4" />
              The search failed.
            </p>
            <p className="mt-1">{state.error.message}</p>
            {state.error instanceof ApiError && state.error.code === 'semantic_search_unavailable' && (
              <button
                type="button"
                onClick={() => update({ mode: 'keyword' })}
                className="mt-2 font-medium underline focus-visible:outline-2 focus-visible:outline-rose-600"
              >
                Search by keyword instead
              </button>
            )}
            {state.error instanceof ApiError && state.error.requestId && (
              <p className="mt-2 text-xs text-rose-700">Request ID: {state.error.requestId}</p>
            )}
          </div>
        )}

        {state.status === 'done' && (
          <div className="space-y-4">
            <div className="flex flex-wrap items-baseline justify-between gap-2">
              <p className="text-sm text-slate-700" aria-live="polite">
                <span className="font-semibold">{pluralize(state.response.results.length, 'result')}</span>{' '}
                for “{state.response.query}” ·{' '}
                <span className="font-medium">{activeMode.label} search</span>
                {state.response.results.length > 0 && (
                  <>, ranked by {SCORE_LABELS[state.response.score_type]}</>
                )}
              </p>
              {state.response.timings_ms.total_ms !== undefined && (
                <p className="text-xs text-slate-500">
                  {Math.round(state.response.timings_ms.total_ms)} ms
                </p>
              )}
            </div>

            {state.response.warnings.length > 0 && (
              <ul className="space-y-1 rounded-md bg-amber-50 p-3 text-sm text-amber-900">
                {state.response.warnings.map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            )}

            {state.response.results.length === 0 ? (
              <div className="rounded-xl border border-slate-200 bg-white px-5 py-10 text-center">
                <p className="text-sm font-medium text-slate-800">No matching passages</p>
                <p className="mt-1 text-sm text-slate-500">
                  {resultMode === 'keyword'
                    ? 'Try other words, or semantic search to match by meaning.'
                    : 'Try different wording, remove filters, or upload more documents.'}
                </p>
              </div>
            ) : (
              <>
                <p className="text-xs text-slate-500">{state.response.score_description}</p>
                <ol className="space-y-3">
                  {state.response.results.map((result) => (
                    <li key={result.chunk_id}>
                      <SearchResultCard result={result} scoreType={state.response.score_type} />
                    </li>
                  ))}
                </ol>
              </>
            )}
          </div>
        )}
      </section>
    </div>
  )
}
