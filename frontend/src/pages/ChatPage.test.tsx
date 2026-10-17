import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from '../lib/api'
import * as chat from '../lib/chat'
import ChatPage from './ChatPage'

const OPTIONS: chat.ChatOptions = {
  configured: true,
  provider: 'anthropic',
  model: 'test-model',
  max_question_chars: 4000,
  retrieval_modes: ['semantic', 'hybrid', 'keyword'],
  default_retrieval_mode: 'hybrid',
  retrieval_top_k: 8,
  max_output_tokens: 2048,
}

const SOURCE: chat.Source = {
  index: 1,
  chunk_id: 'chunk-1',
  document_id: 'doc-1',
  filename: '就業規則.pdf',
  chunk_ordinal: 4,
  page_start: 3,
  page_end: 3,
  section: '在宅勤務',
  language: 'ja',
  retrieval_rank: 1,
  score: 0.03,
  score_type: 'rrf',
  excerpt: '在宅勤務は週に2日までとする。',
}

function summary(overrides: Partial<chat.ConversationSummary> = {}): chat.ConversationSummary {
  return {
    id: 'conv-1',
    title: '在宅勤務',
    created_at: '2026-10-12T10:00:00Z',
    updated_at: '2026-10-12T10:05:00Z',
    message_count: 2,
    ...overrides,
  }
}

function message(overrides: Partial<chat.ChatMessage>): chat.ChatMessage {
  return {
    id: 'm',
    sequence: 1,
    role: 'user',
    content: '',
    status: 'complete',
    created_at: '2026-10-12T10:00:00Z',
    completed_at: '2026-10-12T10:00:00Z',
    client_message_id: null,
    reply_to_id: null,
    grounding: null,
    error_code: null,
    error_message: null,
    requested_provider: null,
    requested_model: null,
    provider: null,
    model: null,
    served_model: null,
    stop_reason: null,
    usage: null,
    retrieval_mode: null,
    timings_ms: {},
    citations: [],
    ...overrides,
  }
}

function detail(messages: chat.ChatMessage[], overrides: Partial<chat.ConversationDetail> = {}) {
  return { ...summary(), messages, messages_truncated: false, ...overrides }
}

const QUESTION = message({ id: 'u1', content: '在宅勤務は週に何日？', client_message_id: 'client-1' })
const ANSWER = message({
  id: 'a1',
  sequence: 2,
  role: 'assistant',
  content: '週に2日までです [S1]。',
  reply_to_id: 'u1',
  grounding: 'grounded',
  provider: 'anthropic',
  model: 'test-model',
  served_model: 'test-model',
  stop_reason: 'end_turn',
  usage: { input_tokens: 900, output_tokens: 40, cache_read_input_tokens: 0, cache_creation_input_tokens: 0 },
  timings_ms: { total_ms: 2100 },
  citations: [
    {
      ...SOURCE,
      supporting: true,
      source_status: 'available',
      current_chunk_id: 'chunk-1',
      current_chunk_ordinal: 4,
    },
  ],
})

function entry(overrides: Partial<chat.ModelEntry> & chat.ModelChoice): chat.ModelEntry {
  return {
    provider_label: overrides.provider,
    cost: 'paid',
    configured: true,
    availability: { state: 'available', reason: null },
    selectable: true,
    default: false,
    ...overrides,
  }
}

const CATALOG: chat.ModelCatalog = {
  providers: [
    {
      name: 'anthropic',
      label: 'Anthropic Claude',
      cost: 'paid',
      configured: true,
      reason: null,
      notice: 'Paid API: each question is billed to the server\'s Anthropic account.',
    },
    {
      name: 'gemini',
      label: 'Google Gemini',
      cost: 'free_tier',
      configured: true,
      reason: null,
      notice: 'Free tier: low rate limits, and Google may use prompts and answers to improve its products.',
    },
    {
      name: 'ollama',
      label: 'Ollama (local)',
      cost: 'local',
      configured: false,
      reason: 'OLLAMA_BASE_URL is not set',
      notice: null,
    },
  ],
  models: [
    entry({ provider: 'anthropic', provider_label: 'Anthropic Claude', model: 'test-model', default: true }),
    entry({
      provider: 'gemini',
      provider_label: 'Google Gemini',
      model: 'gemini-3.8-flash',
      cost: 'free_tier',
      availability: { state: 'unknown', reason: null },
    }),
    entry({
      provider: 'gemini',
      provider_label: 'Google Gemini',
      model: 'gemini-retired',
      cost: 'free_tier',
      availability: { state: 'unavailable', reason: 'Google does not list this model ID.' },
      selectable: false,
    }),
    entry({
      provider: 'ollama',
      provider_label: 'Ollama (local)',
      model: 'qwen3:8b',
      cost: 'local',
      configured: false,
      availability: { state: 'unavailable', reason: 'OLLAMA_BASE_URL is not set' },
      selectable: false,
    }),
  ],
  default: { provider: 'anthropic', model: 'test-model' },
}

function LocationProbe() {
  const { pathname } = useLocation()
  return <output data-testid="location">{pathname}</output>
}

function renderPage(
  url = '/chat',
  {
    options = OPTIONS,
    conversations = [summary()],
    catalog = CATALOG,
  }: {
    options?: chat.ChatOptions
    conversations?: chat.ConversationSummary[]
    catalog?: chat.ModelCatalog | Error
  } = {},
) {
  vi.spyOn(chat, 'fetchChatOptions').mockResolvedValue(options)
  const models =
    catalog instanceof Error
      ? vi.spyOn(chat, 'fetchChatModels').mockRejectedValue(catalog)
      : vi.spyOn(chat, 'fetchChatModels').mockResolvedValue(catalog)
  const list = vi
    .spyOn(chat, 'listConversations')
    .mockResolvedValue({ items: conversations, total: conversations.length, limit: 50, offset: 0 })
  const page = (
    <>
      <ChatPage />
      <LocationProbe />
    </>
  )
  render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/chat" element={page} />
        <Route path="/chat/:conversationId" element={page} />
      </Routes>
    </MemoryRouter>,
  )
  return { list, models }
}

const input = () => screen.getByRole('textbox', { name: 'Your question' })

/** A streamAnswer double that emits the given events and then waits for `release`. */
function controllableStream() {
  let emit: (event: chat.ChatStreamEvent) => void = () => {}
  let finish: () => void = () => {}
  let signal: AbortSignal | null = null
  const spy = vi.spyOn(chat, 'streamAnswer').mockImplementation((_id, _body, onEvent, abort) => {
    emit = onEvent
    signal = abort
    return new Promise<void>((resolve, reject) => {
      finish = resolve
      abort.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')))
    })
  })
  return {
    spy,
    emit: (event: chat.ChatStreamEvent) => emit(event),
    finish: () => finish(),
    signal: () => signal,
  }
}

describe('ChatPage', () => {
  it('starts a conversation, streams the answer and opens the cited passage', async () => {
    const user = userEvent.setup()
    const create = vi.spyOn(chat, 'createConversation').mockResolvedValue(summary())
    const get = vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, ANSWER]))
    const stream = controllableStream()
    renderPage()

    expect(await screen.findByText('Ask your documents')).toBeInTheDocument()
    expect(await screen.findByText('test-model')).toBeInTheDocument()
    await user.type(input(), '在宅勤務は週に何日？{Enter}')

    await waitFor(() => expect(stream.spy).toHaveBeenCalled())
    expect(create).toHaveBeenCalledOnce()
    expect(screen.getByTestId('location')).toHaveTextContent('/chat/conv-1')
    const [conversationId, body] = stream.spy.mock.calls[0]
    expect(conversationId).toBe('conv-1')
    expect(body.content).toBe('在宅勤務は週に何日？')
    expect(body.client_message_id).toBeTruthy()
    expect(input()).toHaveValue('')
    expect(screen.getByText('Searching your documents…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument()

    stream.emit({
      event: 'start',
      data: {
        request_id: 'req-1',
        conversation_id: 'conv-1',
        user_message_id: 'u1',
        assistant_message_id: 'a1',
        provider: 'anthropic',
        model: 'test-model',
        replayed: false,
      },
    })
    stream.emit({
      event: 'sources',
      data: {
        retrieval: { mode: 'hybrid', semantic_available: true, warnings: [], duration_ms: 12 },
        sources: [SOURCE],
      },
    })
    stream.emit({ event: 'delta', data: { text: '週に2日' } })
    // The exact text is the live answer's (the stored answer, loaded meanwhile, is longer).
    expect(await screen.findByText('週に2日')).toBeInTheDocument()
    expect(screen.getByText(/Retrieved passages/)).toBeInTheDocument()

    stream.emit({ event: 'delta', data: { text: 'までです [S1]。' } })
    stream.emit({
      event: 'citations',
      data: { citations: [{ ...SOURCE, supporting: true }], invalid_markers_removed: 0 },
    })
    stream.emit({
      event: 'done',
      data: {
        assistant_message_id: 'a1',
        status: 'complete',
        grounding: 'grounded',
        stop_reason: 'end_turn',
        served_model: 'test-model',
        timings_ms: { total_ms: 2100 },
      },
    })
    stream.finish()

    // The stored version replaces the live one.
    await waitFor(() => expect(get).toHaveBeenCalledWith('conv-1', undefined))
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop' })).not.toBeInTheDocument())
    const answer = screen.getByRole('article', { name: 'Answer' })
    expect(within(answer).getByText('Cited sources (1)')).toBeInTheDocument()
    expect(within(answer).getByText(/Answered by test-model/)).toBeInTheDocument()
    expect(within(answer).getByText(/940 tokens/)).toBeInTheDocument()

    await user.click(within(answer).getByRole('button', { name: 'Show source 1' }))
    const link = await within(answer).findByRole('link', { name: 'Open this passage in the document' })
    expect(link).toHaveAttribute('href', '/documents/doc-1?chunk=4')
    expect(within(answer).getByText('在宅勤務は週に2日までとする。')).toBeInTheDocument()
  })

  it('stops a streaming answer', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION]))
    const stream = controllableStream()
    renderPage('/chat/conv-1')

    await user.type(await screen.findByRole('textbox', { name: 'Your question' }), 'question{Enter}')
    await waitFor(() => expect(stream.spy).toHaveBeenCalled())
    await user.click(screen.getByRole('button', { name: 'Stop' }))

    expect(stream.signal()?.aborted).toBe(true)
    await waitFor(() => expect(screen.getByText('Answer stopped.')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument())
  })

  it('shows an error and restores the question when the server refuses it', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    vi.spyOn(chat, 'streamAnswer').mockRejectedValue(
      new ApiError(409, 'answer_in_progress', 'Another answer is being generated in this conversation.'),
    )
    renderPage('/chat/conv-1')

    await user.type(await screen.findByRole('textbox', { name: 'Your question' }), 'question{Enter}')

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Another answer is being generated in this conversation.',
    )
    expect(input()).toHaveValue('question')
  })

  it('reports a lost connection after the answer started', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    vi.spyOn(chat, 'streamAnswer').mockImplementation(async (_id, _body, onEvent) => {
      onEvent({ event: 'delta', data: { text: 'partial' } })
      throw new TypeError('network error')
    })
    renderPage('/chat/conv-1')

    await user.type(await screen.findByRole('textbox', { name: 'Your question' }), 'question{Enter}')
    expect(await screen.findByText('The connection was lost while the answer was streaming.')).toBeInTheDocument()
    expect(input()).toHaveValue('')
  })

  it('sends with Enter but not with Shift+Enter or during IME composition', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    const stream = vi.spyOn(chat, 'streamAnswer').mockResolvedValue()
    renderPage('/chat/conv-1')

    const box = await screen.findByRole('textbox', { name: 'Your question' })
    await user.type(box, 'line one{Shift>}{Enter}{/Shift}line two')
    expect(box).toHaveValue('line one\nline two')

    fireEvent.keyDown(box, { key: 'Enter', isComposing: true })
    expect(stream).not.toHaveBeenCalled()

    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(stream).toHaveBeenCalledOnce())
    expect(stream.mock.calls[0][1].content).toBe('line one\nline two')
  })

  it('refuses a question over the length limit without sending it', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    const stream = vi.spyOn(chat, 'streamAnswer').mockResolvedValue()
    renderPage('/chat/conv-1', { options: { ...OPTIONS, max_question_chars: 5 } })

    await user.type(await screen.findByRole('textbox', { name: 'Your question' }), 'too long{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('at most 5 characters')
    expect(stream).not.toHaveBeenCalled()
  })

  it('explains when answer generation is not configured', async () => {
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, ANSWER]))
    renderPage('/chat/conv-1', { options: { ...OPTIONS, configured: false } })

    expect(await screen.findByText(/Answer generation is not configured on the server/)).toBeInTheDocument()
    expect(input()).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    // Earlier conversations stay readable.
    expect(await screen.findByText('在宅勤務は週に何日？')).toBeInTheDocument()
  })

  it('shows stored history, and retries a failed answer with the same question id', async () => {
    const user = userEvent.setup()
    const failed = message({
      ...ANSWER,
      content: '',
      status: 'failed',
      grounding: null,
      error_code: 'provider_overloaded',
      error_message: 'The model provider is overloaded. Try again shortly.',
      citations: [],
    })
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, failed]))
    const stream = vi.spyOn(chat, 'streamAnswer').mockResolvedValue()
    renderPage('/chat/conv-1')

    expect(await screen.findByText('在宅勤務は週に何日？')).toBeInTheDocument()
    expect(screen.getByText('The model provider is overloaded. Try again shortly.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() => expect(stream).toHaveBeenCalledOnce())
    expect(stream.mock.calls[0][1]).toMatchObject({ content: '在宅勤務は週に何日？', client_message_id: 'client-1' })
  })

  it('says when a conversation does not exist', async () => {
    vi.spyOn(chat, 'getConversation').mockRejectedValue(new ApiError(404, 'not_found', 'Not found'))
    renderPage('/chat/missing')

    expect(await screen.findByText(/This conversation does not exist/)).toBeInTheDocument()
  })

  it('shows deleted and reindexed sources in stored answers', async () => {
    const user = userEvent.setup()
    const answer = message({
      ...ANSWER,
      content: '[S1] と [S2]',
      citations: [
        { ...ANSWER.citations[0], source_status: 'deleted', excerpt: null, current_chunk_id: null, current_chunk_ordinal: null },
        { ...ANSWER.citations[0], index: 2, chunk_id: 'old', source_status: 'relocated', current_chunk_id: 'new', current_chunk_ordinal: 9 },
      ],
    })
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, answer]))
    renderPage('/chat/conv-1')

    const article = await screen.findByRole('article', { name: 'Answer' })
    await user.click(within(article).getByRole('button', { name: 'Show source 1' }))
    expect(await within(article).findByText('The document has been deleted.')).toBeInTheDocument()
    expect(within(article).getByText('The passage text is no longer stored.')).toBeInTheDocument()

    await user.click(within(article).getByRole('button', { name: 'Show source 2' }))
    const links = await within(article).findAllByRole('link', { name: 'Open this passage in the document' })
    expect(links).toHaveLength(1)
    expect(links[0]).toHaveAttribute('href', '/documents/doc-1?chunk=9')
  })

  it('renames and deletes conversations from the sidebar', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, ANSWER]))
    const rename = vi.spyOn(chat, 'renameConversation').mockResolvedValue(summary({ title: 'Remote work' }))
    const remove = vi.spyOn(chat, 'deleteConversation').mockResolvedValue()
    const { list } = renderPage('/chat/conv-1')

    const sidebar = await screen.findByRole('navigation', { name: 'Conversations' })
    await user.click(await within(sidebar).findByRole('button', { name: 'Rename 在宅勤務' }))
    const title = within(sidebar).getByRole('textbox', { name: 'Conversation title' })
    await user.clear(title)
    await user.type(title, 'Remote work{Enter}')
    await waitFor(() => expect(rename).toHaveBeenCalledWith('conv-1', 'Remote work'))
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(2)

    await user.click(within(sidebar).getByRole('button', { name: 'Delete 在宅勤務' }))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(remove).toHaveBeenCalledWith('conv-1'))
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent(/^\/chat$/))
  })
})

describe('ChatPage model selection', () => {
  beforeEach(() => window.sessionStorage.clear())

  const modelSelect = () => screen.getByRole('combobox', { name: 'Model' })

  it('lists providers with cost and availability, and explains unavailable models', async () => {
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    renderPage('/chat/conv-1')

    await screen.findByRole('combobox', { name: 'Model' })
    const groups = within(modelSelect()).getAllByRole('group')
    expect(groups.map((g) => g.getAttribute('label'))).toEqual([
      'Anthropic Claude · Paid',
      'Google Gemini · Free tier',
      'Ollama (local) · Local',
    ])
    expect(modelSelect()).toHaveValue('anthropic|test-model')
    expect(screen.getByRole('option', { name: 'test-model (default)' })).toBeEnabled()
    expect(screen.getByRole('option', { name: 'gemini-retired — unavailable' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'qwen3:8b — not configured' })).toBeDisabled()
    expect(screen.getByText('Available')).toBeInTheDocument()
    expect(screen.getByText(/billed to the server's Anthropic account/)).toBeInTheDocument()

    expect(screen.getByText('Why 2 models are unavailable')).toBeInTheDocument()
    expect(screen.getByText(/Google does not list this model ID/)).toBeInTheDocument()
    expect(screen.getByText(/OLLAMA_BASE_URL is not set/)).toBeInTheDocument()
  })

  it('sends the chosen model, switches between messages and remembers the choice', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, ANSWER]))
    const stream = vi.spyOn(chat, 'streamAnswer').mockResolvedValue()
    renderPage('/chat/conv-1')

    await user.selectOptions(await screen.findByRole('combobox', { name: 'Model' }), 'gemini|gemini-3.8-flash')
    expect(screen.getByText('Free tier')).toBeInTheDocument()
    expect(screen.getByText('Availability not confirmed')).toBeInTheDocument()
    expect(screen.getByText(/Google may use prompts and answers/)).toBeInTheDocument()
    await user.type(input(), 'first question{Enter}')
    await waitFor(() => expect(stream).toHaveBeenCalledOnce())
    expect(stream.mock.calls[0][1].model).toEqual({ provider: 'gemini', model: 'gemini-3.8-flash' })
    expect(JSON.parse(window.sessionStorage.getItem('yashrag.chat.model') ?? '{}')).toEqual({
      provider: 'gemini',
      model: 'gemini-3.8-flash',
    })

    // Same conversation, another model for the next question.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument())
    await user.selectOptions(modelSelect(), 'anthropic|test-model')
    await user.type(input(), 'second question{Enter}')
    await waitFor(() => expect(stream).toHaveBeenCalledTimes(2))
    expect(stream.mock.calls[1][0]).toBe('conv-1')
    expect(stream.mock.calls[1][1].model).toEqual({ provider: 'anthropic', model: 'test-model' })
  })

  it('restores the remembered model for this browser session', async () => {
    window.sessionStorage.setItem(
      'yashrag.chat.model',
      JSON.stringify({ provider: 'gemini', model: 'gemini-3.8-flash' }),
    )
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    renderPage('/chat/conv-1')
    await waitFor(() => expect(modelSelect()).toHaveValue('gemini|gemini-3.8-flash'))
  })

  it('does not switch on its own when the remembered model became unavailable', async () => {
    const user = userEvent.setup()
    window.sessionStorage.setItem(
      'yashrag.chat.model',
      JSON.stringify({ provider: 'gemini', model: 'gemini-retired' }),
    )
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    const stream = vi.spyOn(chat, 'streamAnswer').mockResolvedValue()
    renderPage('/chat/conv-1')

    await waitFor(() => expect(modelSelect()).toHaveValue('gemini|gemini-retired'))
    expect(screen.getByRole('alert')).toHaveTextContent(
      'gemini-retired cannot be used: Google does not list this model ID. Choose another model.',
    )
    await user.type(input(), 'question{Enter}')
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    expect(stream).not.toHaveBeenCalled()
  })

  it('refreshes the model list when the server says the model is unavailable', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    vi.spyOn(chat, 'streamAnswer').mockRejectedValue(
      new ApiError(503, 'model_unavailable', 'test-model is not available: retired.'),
    )
    const { models } = renderPage('/chat/conv-1')

    await screen.findByRole('combobox', { name: 'Model' })
    await user.type(input(), 'question{Enter}')
    expect(await screen.findByText('test-model is not available: retired.')).toBeInTheDocument()
    expect(input()).toHaveValue('question')
    await waitFor(() => expect(models).toHaveBeenLastCalledWith(true))
  })

  it('checks availability again on request', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    const { models } = renderPage('/chat/conv-1')
    await user.click(await screen.findByRole('button', { name: 'Check again' }))
    await waitFor(() => expect(models).toHaveBeenLastCalledWith(true))
  })

  it('falls back to the server default when the model list cannot be loaded', async () => {
    const user = userEvent.setup()
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    const stream = vi.spyOn(chat, 'streamAnswer').mockResolvedValue()
    renderPage('/chat/conv-1', { catalog: new Error('timed out') })

    expect(await screen.findByText(/The model list could not be loaded \(timed out\)/)).toBeInTheDocument()
    expect(screen.queryByRole('combobox', { name: 'Model' })).not.toBeInTheDocument()
    await user.type(input(), 'question{Enter}')
    await waitFor(() => expect(stream).toHaveBeenCalledOnce())
    expect(stream.mock.calls[0][1]).not.toHaveProperty('model')
  })

  it('says when no model can be used', async () => {
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([]))
    renderPage('/chat/conv-1', {
      catalog: { ...CATALOG, models: CATALOG.models.map((m) => ({ ...m, selectable: false })) },
    })
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'No configured model is available on the server.',
    )
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
  })

  it('shows which provider and model answered each stored message', async () => {
    const gemini = message({
      ...ANSWER,
      id: 'a2',
      sequence: 4,
      reply_to_id: 'u2',
      requested_provider: 'gemini',
      requested_model: 'gemini-3.8-flash',
      provider: 'gemini',
      model: 'gemini-3.8-flash',
      served_model: 'gemini-3.8-flash-001',
    })
    const second = message({ id: 'u2', sequence: 3, content: 'follow-up' })
    vi.spyOn(chat, 'getConversation').mockResolvedValue(detail([QUESTION, ANSWER, second, gemini]))
    renderPage('/chat/conv-1')
    expect(await screen.findByText(/Answered by test-model \(Claude\)/)).toBeInTheDocument()
    expect(
      screen.getByText(/Answered by gemini-3.8-flash-001 \(Gemini\), fallback from gemini-3.8-flash/),
    ).toBeInTheDocument()
  })
})
