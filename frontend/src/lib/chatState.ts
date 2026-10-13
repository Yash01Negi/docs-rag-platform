// State of the chat page: the answer being streamed, and stored answers, in one shape.

import type {
  ChatMessage,
  ChatStreamEvent,
  Citation,
  Grounding,
  MessageStatus,
  Source,
  StreamCitation,
  Usage,
} from './chat'

/** A source as shown under an answer: live (from the stream) or stored (with its status). */
export type DisplaySource = Source &
  Partial<Pick<StreamCitation, 'supporting'>> &
  Partial<Pick<Citation, 'source_status' | 'current_chunk_id' | 'current_chunk_ordinal'>>

export interface AssistantView {
  id: string
  text: string
  /** 'retrieving' and 'streaming' only occur while an answer is being received. */
  status: MessageStatus | 'retrieving'
  grounding: Grounding | null
  sources: DisplaySource[]
  /** Cited source numbers; null until the server has checked the citations. */
  cited: number[] | null
  provider: string | null
  model: string | null
  servedModel: string | null
  stopReason: string | null
  usage: Usage | null
  totalMs: number | null
  error: { code: string; message: string; retryable: boolean } | null
  warnings: string[]
}

/** A question being answered right now, shown until the stored version is reloaded. */
export interface LiveTurn {
  conversationId: string
  question: string
  clientMessageId: string
  userMessageId: string | null
  answer: AssistantView
}

export function emptyAnswer(): AssistantView {
  return {
    id: 'live',
    text: '',
    status: 'retrieving',
    grounding: null,
    sources: [],
    cited: null,
    provider: null,
    model: null,
    servedModel: null,
    stopReason: null,
    usage: null,
    totalMs: null,
    error: null,
    warnings: [],
  }
}

/** Apply one stream event to the live answer. */
export function applyEvent(turn: LiveTurn, event: ChatStreamEvent): LiveTurn {
  const answer = turn.answer
  switch (event.event) {
    case 'start':
      return {
        ...turn,
        userMessageId: event.data.user_message_id,
        answer: {
          ...answer,
          id: event.data.assistant_message_id,
          provider: event.data.provider,
          model: event.data.model,
        },
      }
    case 'sources':
      return {
        ...turn,
        answer: {
          ...answer,
          status: 'streaming',
          sources: event.data.sources,
          warnings: event.data.retrieval.warnings,
        },
      }
    case 'delta':
      return { ...turn, answer: { ...answer, status: 'streaming', text: answer.text + event.data.text } }
    case 'citations':
      return {
        ...turn,
        answer: {
          ...answer,
          sources: event.data.citations,
          cited: event.data.citations.filter((c) => c.supporting).map((c) => c.index),
        },
      }
    case 'usage':
      return { ...turn, answer: { ...answer, usage: event.data } }
    case 'done':
      return {
        ...turn,
        answer: {
          ...answer,
          status: event.data.status,
          grounding: event.data.grounding,
          servedModel: event.data.served_model,
          stopReason: event.data.stop_reason,
          totalMs: event.data.timings_ms.total_ms ?? null,
        },
      }
    case 'error':
      return {
        ...turn,
        answer: {
          ...answer,
          status: 'failed',
          error: { code: event.data.code, message: event.data.message, retryable: event.data.retryable },
        },
      }
  }
}

export function storedAnswer(message: ChatMessage): AssistantView {
  return {
    id: message.id,
    text: message.content,
    status: message.status,
    grounding: message.grounding,
    sources: message.citations,
    cited: message.citations.filter((c) => c.supporting).map((c) => c.index),
    provider: message.provider,
    model: message.model,
    servedModel: message.served_model,
    stopReason: message.stop_reason,
    usage: message.usage,
    totalMs: message.timings_ms.total_ms ?? null,
    error: message.error_message
      ? { code: message.error_code ?? 'error', message: message.error_message, retryable: true }
      : null,
    warnings: [],
  }
}

