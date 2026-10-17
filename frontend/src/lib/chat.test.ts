import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApiError } from './api'
import { SseParser, citedIndices, fetchChatModels, streamAnswer, type ChatStreamEvent } from './chat'

afterEach(() => vi.restoreAllMocks())

describe('SseParser', () => {
  it('parses events split across arbitrary chunks', () => {
    const body =
      ': stream open\n\nid: 1\nevent: start\ndata: {"conversation_id":"c"}\n\n' +
      'id: 2\nevent: delta\ndata: {"text":"日本語 [S1]"}\n\n'
    for (let split = 1; split < body.length; split++) {
      const parser = new SseParser()
      const events = [...parser.feed(body.slice(0, split)), ...parser.feed(body.slice(split))]
      expect(events.map((e) => e.event)).toEqual(['start', 'delta'])
      expect(events[1].data).toEqual({ text: '日本語 [S1]' })
    }
  })

  it('handles CRLF line endings, comments, unknown events and bad JSON', () => {
    const parser = new SseParser()
    const events = parser.feed(
      ': keep-alive\r\n\r\nevent: future\r\ndata: {}\r\n\r\nevent: delta\r\ndata: not json\r\n\r\n' +
        'event: delta\r\ndata: {"text":"ok"}\r\n\r\n',
    )
    expect(events).toEqual([{ event: 'delta', data: { text: 'ok' } }])
  })

  it('joins multi-line data fields', () => {
    const parser = new SseParser()
    const events = parser.feed('event: delta\ndata: {"text":\ndata: "a"}\n\n')
    expect(events).toEqual([{ event: 'delta', data: { text: 'a' } }])
  })
})

describe('streamAnswer', () => {
  function sseResponse(chunks: string[]): Response {
    const encoder = new TextEncoder()
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(encoder.encode(chunk))
        controller.close()
      },
    })
    return new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } })
  }

  it('posts the question and delivers events in order', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(
        sseResponse(['event: delta\ndata: {"text":"a', 'b"}\n\nevent: done\ndata: {"status":"complete"}\n\n']),
      )
    const events: ChatStreamEvent[] = []
    await streamAnswer(
      'conv-1',
      { content: '質問', client_message_id: 'id-1' },
      (event) => events.push(event),
      new AbortController().signal,
    )
    expect(events.map((e) => e.event)).toEqual(['delta', 'done'])
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/v1/conversations/conv-1/messages')
    expect(JSON.parse(init?.body as string)).toEqual({ content: '質問', client_message_id: 'id-1' })
    expect(init?.headers).toMatchObject({ Accept: 'text/event-stream' })
  })

  it('turns an error response into an ApiError', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(
        JSON.stringify({ error: { code: 'conversation_busy', message: 'Busy.', request_id: 'r1' } }),
        { status: 409 },
      ),
    )
    const promise = streamAnswer('c', { content: 'q', client_message_id: 'x' }, () => {}, new AbortController().signal)
    await expect(promise).rejects.toBeInstanceOf(ApiError)
    await expect(promise).rejects.toMatchObject({ status: 409, code: 'conversation_busy', requestId: 'r1' })
  })
})

describe('model choice', () => {
  it('sends the chosen provider and model with the question', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValue(new Response('event: done\ndata: {"status":"complete"}\n\n', { status: 200 }))
    await streamAnswer(
      'conv-1',
      { content: 'q', client_message_id: 'id-1', model: { provider: 'ollama', model: 'qwen3:8b' } },
      () => {},
      new AbortController().signal,
    )
    expect(JSON.parse(fetchMock.mock.calls[0][1]?.body as string)).toEqual({
      content: 'q',
      client_message_id: 'id-1',
      model: { provider: 'ollama', model: 'qwen3:8b' },
    })
  })

  it('fetches the catalog, optionally asking for a fresh check', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async () => new Response(JSON.stringify({ providers: [], models: [], default: null })))
    await fetchChatModels()
    await fetchChatModels(true)
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual([
      '/api/v1/chat/models',
      '/api/v1/chat/models?refresh=true',
    ])
  })
})

describe('citedIndices', () => {
  it('lists cited sources once, in order', () => {
    expect(citedIndices('a [S2] b [S1][S2] c [Sx]')).toEqual([2, 1])
  })
})
