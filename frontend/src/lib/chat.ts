// Chat API client (Phase 4): conversations and the Server-Sent Events answer stream.

import { ApiError, type Language, type SearchMode } from './api'

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/$/, '')
const REQUEST_TIMEOUT_MS = 10_000

export type MessageStatus = 'complete' | 'streaming' | 'failed' | 'cancelled'
export type Grounding = 'grounded' | 'insufficient_evidence' | 'no_sources' | 'uncited'
export type SourceStatus = 'available' | 'relocated' | 'changed' | 'deleted'

export interface ConversationSummary {
  id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface ConversationList {
  items: ConversationSummary[]
  total: number
  limit: number
  offset: number
}

/** A retrieved passage given to the model, as sent in the `sources` event. */
export interface Source {
  index: number
  chunk_id: string
  document_id: string
  filename: string
  chunk_ordinal: number
  page_start: number | null
  page_end: number | null
  section: string | null
  language: Language
  retrieval_rank: number
  score: number
  score_type: string
  excerpt: string | null
}

export interface StreamCitation extends Source {
  supporting: boolean
}

/** A stored citation, with where the passage is now. */
export interface Citation extends StreamCitation {
  source_status: SourceStatus
  current_chunk_id: string | null
  current_chunk_ordinal: number | null
}

export interface Usage {
  input_tokens: number
  output_tokens: number
  cache_read_input_tokens: number
  cache_creation_input_tokens: number
}

export interface ChatMessage {
  id: string
  sequence: number
  role: 'user' | 'assistant'
  content: string
  status: MessageStatus
  created_at: string
  completed_at: string | null
  client_message_id: string | null
  reply_to_id: string | null
  grounding: Grounding | null
  error_code: string | null
  error_message: string | null
  /** What the question asked for (its model choice, or the server default). */
  requested_provider: string | null
  requested_model: string | null
  /** What was called; null when no model was called (no passage matched). */
  provider: string | null
  model: string | null
  served_model: string | null
  stop_reason: string | null
  usage: Usage | null
  retrieval_mode: string | null
  timings_ms: Record<string, number>
  citations: Citation[]
}

export interface ConversationDetail extends ConversationSummary {
  messages: ChatMessage[]
  messages_truncated: boolean
}

export interface ChatOptions {
  configured: boolean
  provider: string
  model: string | null
  max_question_chars: number
  retrieval_modes: SearchMode[]
  default_retrieval_mode: SearchMode
  retrieval_top_k: number
  max_output_tokens: number
}

// --- model catalog (Phase 4.1) ----------------------------------------------------------------

export type ModelCost = 'paid' | 'free_tier' | 'local'
export type AvailabilityState = 'available' | 'unavailable' | 'unknown'

/** A provider/model pair as the server lists it; sent back unchanged with a question. */
export interface ModelChoice {
  provider: string
  model: string
}

export interface ProviderEntry {
  name: string
  label: string
  cost: ModelCost
  configured: boolean
  reason: string | null
  notice: string | null
}

export interface ModelEntry extends ModelChoice {
  provider_label: string
  cost: ModelCost
  configured: boolean
  availability: { state: AvailabilityState; reason: string | null }
  selectable: boolean
  default: boolean
}

export interface ModelCatalog {
  providers: ProviderEntry[]
  models: ModelEntry[]
  default: ModelChoice | null
}

interface ErrorEnvelope {
  error?: { code?: string; message?: string; request_id?: string | null }
}

async function toApiError(response: Response): Promise<ApiError> {
  let body: ErrorEnvelope = {}
  try {
    body = (await response.json()) as ErrorEnvelope
  } catch {
    // Not JSON; use the status line.
  }
  return new ApiError(
    response.status,
    body.error?.code ?? 'http_error',
    body.error?.message ?? `Request failed with HTTP ${response.status}`,
    body.error?.request_id ?? response.headers.get('X-Request-ID'),
  )
}

async function request<T>(path: string, init: RequestInit = {}, ok = [200]): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
    signal: init.signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
  if (!ok.includes(response.status)) throw await toApiError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

const json = (body: unknown): RequestInit => ({
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export function fetchChatOptions(): Promise<ChatOptions> {
  return request<ChatOptions>('/api/v1/chat/options')
}

/** The models the server offers. `refresh` asks the server to check availability again. */
export function fetchChatModels(refresh = false): Promise<ModelCatalog> {
  return request<ModelCatalog>(`/api/v1/chat/models${refresh ? '?refresh=true' : ''}`, {
    // Availability checks contact the providers; allow them more time than other calls.
    signal: AbortSignal.timeout(30_000),
  })
}

export function listConversations(limit = 50, offset = 0): Promise<ConversationList> {
  return request<ConversationList>(`/api/v1/conversations?limit=${limit}&offset=${offset}`)
}

export function createConversation(title?: string): Promise<ConversationSummary> {
  return request<ConversationSummary>(
    '/api/v1/conversations',
    { method: 'POST', ...json(title ? { title } : {}) },
    [201],
  )
}

export function getConversation(id: string, signal?: AbortSignal): Promise<ConversationDetail> {
  return request<ConversationDetail>(`/api/v1/conversations/${encodeURIComponent(id)}`, {
    signal: signal ?? AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  })
}

export function renameConversation(id: string, title: string): Promise<ConversationSummary> {
  return request<ConversationSummary>(`/api/v1/conversations/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    ...json({ title }),
  })
}

export function deleteConversation(id: string): Promise<void> {
  return request<void>(
    `/api/v1/conversations/${encodeURIComponent(id)}`,
    { method: 'DELETE' },
    [204],
  )
}

// --- Server-Sent Events ---------------------------------------------------------------------

export type ChatStreamEvent =
  | {
      event: 'start'
      data: {
        request_id: string | null
        conversation_id: string
        user_message_id: string
        assistant_message_id: string
        provider: string | null
        model: string | null
        replayed: boolean
      }
    }
  | {
      event: 'sources'
      data: {
        retrieval: {
          mode: string
          semantic_available: boolean
          warnings: string[]
          duration_ms: number
        }
        sources: Source[]
      }
    }
  | { event: 'delta'; data: { text: string } }
  | { event: 'citations'; data: { citations: StreamCitation[]; invalid_markers_removed: number } }
  | { event: 'usage'; data: Usage }
  | {
      event: 'done'
      data: {
        assistant_message_id: string
        status: MessageStatus
        grounding: Grounding | null
        stop_reason: string | null
        served_model: string | null
        timings_ms: Record<string, number>
      }
    }
  | {
      event: 'error'
      data: {
        code: string
        message: string
        retryable: boolean
        retry_after_seconds: number | null
        assistant_message_id: string | null
        request_id: string | null
      }
    }

const KNOWN_EVENTS = new Set(['start', 'sources', 'delta', 'citations', 'usage', 'done', 'error'])

/**
 * Incremental SSE parser: feed decoded text, get complete events. Comments (keep-alives)
 * and unknown event types are ignored, so the server can add events without breaking clients.
 */
export class SseParser {
  private buffer = ''

  feed(text: string): ChatStreamEvent[] {
    this.buffer += text.replace(/\r\n?/g, '\n')
    const events: ChatStreamEvent[] = []
    let boundary = this.buffer.indexOf('\n\n')
    while (boundary !== -1) {
      const block = this.buffer.slice(0, boundary)
      this.buffer = this.buffer.slice(boundary + 2)
      const event = parseBlock(block)
      if (event) events.push(event)
      boundary = this.buffer.indexOf('\n\n')
    }
    return events
  }
}

function parseBlock(block: string): ChatStreamEvent | null {
  let name = 'message'
  const data: string[] = []
  for (const line of block.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') name = value
    else if (field === 'data') data.push(value)
  }
  if (!KNOWN_EVENTS.has(name) || data.length === 0) return null
  try {
    return { event: name, data: JSON.parse(data.join('\n')) } as ChatStreamEvent
  } catch {
    return null
  }
}

export interface AskBody {
  content: string
  client_message_id: string
  retrieval?: { mode?: SearchMode; document_ids?: string[] }
  /** Omitted: the server's default model. */
  model?: ModelChoice
}

/**
 * POST a question and call `onEvent` for each event as it arrives. Resolves when the stream
 * ends; rejects with ApiError for errors returned before the stream starts, and with an
 * AbortError when `signal` is aborted (the server then stops generating).
 */
export async function streamAnswer(
  conversationId: string,
  body: AskBody,
  onEvent: (event: ChatStreamEvent) => void,
  signal: AbortSignal,
): Promise<void> {
  const response = await fetch(
    `${API_BASE_URL}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages`,
    {
      method: 'POST',
      headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal,
    },
  )
  if (!response.ok) throw await toApiError(response)
  if (!response.body) throw new ApiError(0, 'no_stream', 'The browser did not provide a stream.')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  const parser = new SseParser()
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    for (const event of parser.feed(decoder.decode(value, { stream: true }))) onEvent(event)
  }
  for (const event of parser.feed(decoder.decode() + '\n\n')) onEvent(event)
}

// --- citation markers -----------------------------------------------------------------------

const MARKER = /\[S(\d{1,3})\]/g

/** Source numbers cited in an answer, in order of first appearance. */
export function citedIndices(text: string): number[] {
  const seen: number[] = []
  for (const match of text.matchAll(MARKER)) {
    const index = Number(match[1])
    if (!seen.includes(index)) seen.push(index)
  }
  return seen
}

export function newClientMessageId(): string {
  return crypto.randomUUID()
}

/** DOM id of a source in the list under an answer (citation buttons move focus to it). */
export function sourceElementId(messageId: string, index: number): string {
  return `source-${messageId}-${index}`
}
