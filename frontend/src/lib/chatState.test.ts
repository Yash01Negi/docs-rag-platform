import { describe, expect, it } from 'vitest'

import type { Source } from './chat'
import { applyEvent, emptyAnswer, type LiveTurn } from './chatState'

const source: Source = {
  index: 1,
  chunk_id: 'chunk-1',
  document_id: 'doc-1',
  filename: '就業規則.txt',
  chunk_ordinal: 2,
  page_start: null,
  page_end: null,
  section: null,
  language: 'ja',
  retrieval_rank: 1,
  score: 0.8,
  score_type: 'cosine_similarity',
  excerpt: '在宅勤務は週に最大三日まで',
}

describe('applyEvent', () => {
  it('builds the live answer from the stream', () => {
    let turn: LiveTurn = {
      conversationId: 'c',
      question: 'q',
      clientMessageId: 'k',
      userMessageId: null,
      answer: emptyAnswer(),
    }
    turn = applyEvent(turn, {
      event: 'start',
      data: {
        request_id: 'r',
        conversation_id: 'c',
        user_message_id: 'u1',
        assistant_message_id: 'a1',
        provider: 'anthropic',
        model: 'claude-opus-5-5',
        replayed: false,
      },
    })
    expect(turn.userMessageId).toBe('u1')
    expect(turn.answer.id).toBe('a1')
    expect(turn.answer.status).toBe('retrieving')
    turn = applyEvent(turn, {
      event: 'sources',
      data: {
        retrieval: { mode: 'semantic', semantic_available: true, warnings: ['w'], duration_ms: 3 },
        sources: [source],
      },
    })
    expect(turn.answer.status).toBe('streaming')
    expect(turn.answer.cited).toBeNull()
    turn = applyEvent(turn, { event: 'delta', data: { text: '三日 ' } })
    turn = applyEvent(turn, { event: 'delta', data: { text: '[S1]' } })
    expect(turn.answer.text).toBe('三日 [S1]')
    turn = applyEvent(turn, {
      event: 'citations',
      data: { citations: [{ ...source, supporting: true }], invalid_markers_removed: 0 },
    })
    expect(turn.answer.cited).toEqual([1])
    turn = applyEvent(turn, {
      event: 'done',
      data: {
        assistant_message_id: 'a1',
        status: 'complete',
        grounding: 'grounded',
        stop_reason: 'end_turn',
        served_model: 'claude-opus-5-5',
        timings_ms: { total_ms: 1500 },
      },
    })
    expect(turn.answer).toMatchObject({ status: 'complete', grounding: 'grounded', totalMs: 1500 })
  })

  it('records a failure', () => {
    const turn = applyEvent(
      { conversationId: 'c', question: 'q', clientMessageId: 'k', userMessageId: null, answer: emptyAnswer() },
      {
        event: 'error',
        data: {
          code: 'provider_rate_limited',
          message: 'Rate limited.',
          retryable: true,
          retry_after_seconds: 5,
          assistant_message_id: 'a1',
          request_id: 'r',
        },
      },
    )
    expect(turn.answer.status).toBe('failed')
    expect(turn.answer.error).toEqual({ code: 'provider_rate_limited', message: 'Rate limited.', retryable: true })
  })
})
