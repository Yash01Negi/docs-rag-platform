import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as api from '../lib/api'
import { makeDocument } from '../test/fixtures'
import SearchPage from './SearchPage'

function result(overrides: Partial<api.SearchResult> = {}): api.SearchResult {
  return {
    rank: 1,
    score: 0.0328,
    chunk_id: 'chunk-1',
    document_id: 'doc-1',
    filename: '検索ガイド.pdf',
    file_type: 'pdf',
    document_language: 'ja',
    page_count: 12,
    chunk_ordinal: 4,
    language: 'ja',
    page_start: 3,
    page_end: 4,
    section: 'インデックス',
    text: '各チャンクの埋め込みベクトルはPostgreSQLに保存されます。',
    snippet: '各チャンクの埋め込みベクトルはPostgreSQLに保存されます。',
    snippet_start: 0,
    highlights: [[10, 14]],
    retrieval_methods: ['semantic', 'keyword'],
    semantic: { rank: 2, score: 0.81 },
    keyword: { rank: 1, score: 7.42 },
    ...overrides,
  }
}

function response(overrides: Partial<api.SearchResponse> = {}): api.SearchResponse {
  return {
    query: 'ベクトル',
    mode: 'hybrid',
    score_type: 'rrf',
    score_description: 'Reciprocal Rank Fusion score: the sum of 1 / (k + rank).',
    top_k: 10,
    results: [result()],
    query_terms: ['ベクトル'],
    semantic_available: true,
    warnings: [],
    request_id: 'req-1',
    timings_ms: { total_ms: 41.2 },
    ...overrides,
  }
}

function LocationProbe() {
  const { pathname, search } = useLocation()
  return <output data-testid="location">{pathname + search}</output>
}

function renderPage(url = '/search', defaultMode: api.SearchMode = 'semantic') {
  vi.spyOn(api, 'fetchSearchOptions').mockResolvedValue({
    modes: ['semantic', 'keyword', 'hybrid'],
    default_mode: defaultMode,
    default_top_k: 10,
    max_top_k: 50,
    max_query_chars: 1000,
    rrf_k: 60,
    fusion_depth: 50,
  })
  vi.spyOn(api, 'listDocuments').mockResolvedValue({
    items: [makeDocument({ id: 'doc-1', filename: '検索ガイド.pdf' })],
    total: 1,
    limit: 100,
    offset: 0,
  })
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/search"
          element={
            <>
              <SearchPage />
              <LocationProbe />
            </>
          }
        />
        <Route path="/documents/:id" element={<LocationProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

afterEach(() => {
  vi.restoreAllMocks()
})

describe('SearchPage', () => {
  it('shows an empty state with examples before searching', async () => {
    const search = vi.spyOn(api, 'searchDocuments')
    renderPage()
    expect(await screen.findByText('Search your documents')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '埋め込みベクトルの保存' })).toBeInTheDocument()
    expect(search).not.toHaveBeenCalled()
  })

  it("uses the server's default mode and renders ranked results", async () => {
    const search = vi.spyOn(api, 'searchDocuments').mockResolvedValue(response())
    const user = userEvent.setup()
    renderPage('/search', 'hybrid')

    // The selector shows the server's default once the options arrive.
    await waitFor(() => expect(screen.getByLabelText('Hybrid')).toBeChecked())
    await user.type(screen.getByLabelText('Search query'), 'ベクトル{Enter}')

    const card = await screen.findByRole('article')
    // No mode in the request: the server applies SEARCH_DEFAULT_MODE.
    expect(search).toHaveBeenCalledWith({ query: 'ベクトル' }, expect.anything())
    expect(within(card).getByRole('link', { name: '検索ガイド.pdf' })).toHaveAttribute(
      'href',
      '/documents/doc-1?chunk=4',
    )
    expect(within(card).getByText('pp. 3–4')).toBeInTheDocument()
    expect(within(card).getByText('§ インデックス')).toBeInTheDocument()
    expect(within(card).getByText('RRF 0.0328')).toBeInTheDocument()
    expect(within(card).getByText('Semantic #2')).toBeInTheDocument()
    expect(within(card).getByText('Keyword #1')).toBeInTheDocument()
    // The highlight marks exactly the matching word from the API's offsets.
    expect(card.querySelector('mark')?.textContent).toBe('ベクトル')
    expect(screen.getByText('1 result').parentElement).toHaveTextContent('Hybrid search')
    expect(screen.getByTestId('location')).toHaveTextContent(
      '/search?q=%E3%83%99%E3%82%AF%E3%83%88%E3%83%AB',
    )
  })

  it('sends the selected mode and filters', async () => {
    const search = vi
      .spyOn(api, 'searchDocuments')
      .mockResolvedValue(response({ mode: 'keyword', score_type: 'bm25' }))
    const user = userEvent.setup()
    renderPage('/search?q=PostgreSQL')
    await screen.findByRole('article')

    await user.click(screen.getByLabelText('Keyword'))
    await user.selectOptions(await screen.findByLabelText('Document'), 'doc-1')
    await user.selectOptions(screen.getByLabelText('Passage language'), 'ja')

    expect(search).toHaveBeenLastCalledWith(
      { query: 'PostgreSQL', mode: 'keyword', language: 'ja', document_ids: ['doc-1'] },
      expect.anything(),
    )
    expect(screen.getByLabelText('Keyword')).toBeChecked()
  })

  it('lets the keyboard change the mode', async () => {
    vi.spyOn(api, 'searchDocuments').mockResolvedValue(response())
    const user = userEvent.setup()
    renderPage('/search?q=x')
    await screen.findByRole('article')

    expect(screen.getByLabelText('Semantic')).toBeChecked()
    screen.getByLabelText('Semantic').focus()
    await user.keyboard('{ArrowRight}')
    expect(screen.getByLabelText('Hybrid')).toBeChecked()
    expect(screen.getByTestId('location')).toHaveTextContent('mode=hybrid')
  })

  it('shows a message when nothing matches', async () => {
    vi.spyOn(api, 'searchDocuments').mockResolvedValue(response({ results: [] }))
    renderPage('/search?q=zzz')
    expect(await screen.findByText('No matching passages')).toBeInTheDocument()
  })

  it('shows warnings from the API', async () => {
    vi.spyOn(api, 'searchDocuments').mockResolvedValue(
      response({
        semantic_available: false,
        warnings: ['Semantic search is unavailable; these results come from keyword search only.'],
      }),
    )
    renderPage('/search?q=x')
    expect(await screen.findByText(/these results come from keyword search only/)).toBeInTheDocument()
  })

  it('offers keyword search when semantic search is unavailable', async () => {
    const search = vi
      .spyOn(api, 'searchDocuments')
      .mockRejectedValueOnce(
        new api.ApiError(503, 'semantic_search_unavailable', 'Semantic search is unavailable.', 'req-9'),
      )
      .mockResolvedValue(response({ mode: 'keyword', score_type: 'bm25' }))
    const user = userEvent.setup()
    renderPage('/search?q=x&mode=semantic')

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Semantic search is unavailable.')
    expect(alert).toHaveTextContent('Request ID: req-9')
    await user.click(within(alert).getByRole('button', { name: 'Search by keyword instead' }))
    expect(await screen.findByRole('article')).toBeInTheDocument()
    expect(search).toHaveBeenLastCalledWith({ query: 'x', mode: 'keyword' }, expect.anything())
  })

  it('shows the full chunk when the snippet is an excerpt', async () => {
    const text = 'Intro. '.repeat(60) + 'The vector is stored here.'
    vi.spyOn(api, 'searchDocuments').mockResolvedValue(
      response({
        results: [
          result({ text, snippet: 'The vector is stored here.', snippet_start: 420, highlights: [[4, 10]] }),
        ],
      }),
    )
    const user = userEvent.setup()
    renderPage('/search?q=vector')
    const card = await screen.findByRole('article')
    await user.click(within(card).getByText('Show the full chunk'))
    expect(within(card).getByText(/Intro\. Intro\./)).toBeVisible()
  })
})
