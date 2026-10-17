import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent } from 'react'
import { useNavigate, useParams } from 'react-router'

import AssistantMessage from '../components/chat/AssistantMessage'
import ConversationSidebar from '../components/chat/ConversationSidebar'
import ModelSelector from '../components/chat/ModelSelector'
import ConfirmDialog from '../components/documents/ConfirmDialog'
import { AlertIcon, ChatIcon } from '../components/icons'
import { ApiError, type SearchMode } from '../lib/api'
import {
  createConversation,
  deleteConversation,
  fetchChatModels,
  fetchChatOptions,
  getConversation,
  listConversations,
  newClientMessageId,
  renameConversation,
  streamAnswer,
  type ChatMessage,
  type ChatOptions,
  type ConversationDetail,
  type ConversationSummary,
  type ModelCatalog,
  type ModelEntry,
} from '../lib/chat'
import { applyEvent, emptyAnswer, storedAnswer, type LiveTurn } from '../lib/chatState'
import { chooseModel, loadStoredChoice, modelKey, storeChoice } from '../lib/modelSelection'

// Errors that mean the model list shown is out of date.
const MODEL_ERRORS = new Set(['model_unavailable', 'provider_not_configured', 'invalid_model'])

const MODE_LABELS: Record<SearchMode, string> = {
  semantic: 'Semantic',
  hybrid: 'Hybrid',
  keyword: 'Keyword',
}

const EXAMPLES = ['在宅勤務は週に何日までできますか？', 'What is the backup retention period?']

function UserBubble({ text }: { text: string }) {
  return (
    <div className="flex justify-end">
      <p className="max-w-[85%] rounded-2xl rounded-tr-sm bg-indigo-600 px-4 py-2 text-sm whitespace-pre-wrap text-white">
        {text}
      </p>
    </div>
  )
}

export default function ChatPage() {
  const { conversationId } = useParams()
  const navigate = useNavigate()
  const [options, setOptions] = useState<ChatOptions | null>(null)
  const [catalog, setCatalog] = useState<ModelCatalog | null>(null)
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [selectedKey, setSelectedKey] = useState<string | null>(null)
  const [refreshingModels, setRefreshingModels] = useState(false)
  const [conversations, setConversations] = useState<ConversationSummary[] | null>(null)
  const [listError, setListError] = useState<string | null>(null)
  const [detail, setDetail] = useState<ConversationDetail | null>(null)
  // The error is kept with the conversation it belongs to, so switching conversations clears it.
  const [detailError, setDetailError] = useState<{ id: string; error: Error } | null>(null)
  const [live, setLive] = useState<LiveTurn | null>(null)
  const [sendError, setSendError] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [mode, setMode] = useState<SearchMode | ''>('')
  const [toDelete, setToDelete] = useState<ConversationSummary | null>(null)
  const [deleting, setDeleting] = useState(false)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [announcement, setAnnouncement] = useState('')
  const abortRef = useRef<AbortController | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const inputId = useId()
  const modeId = useId()

  const scrollToEnd = useCallback(() => {
    requestAnimationFrame(() => endRef.current?.scrollIntoView?.({ block: 'end' }))
  }, [])

  const refreshList = useCallback(async () => {
    try {
      const page = await listConversations()
      setConversations(page.items)
      setListError(null)
    } catch (caught) {
      setListError(caught instanceof Error ? caught.message : String(caught))
    }
  }, [])

  const loadModels = useCallback(async (refresh = false) => {
    setRefreshingModels(true)
    try {
      const loaded = await fetchChatModels(refresh)
      setCatalog(loaded)
      setCatalogError(null)
      // Keep the current choice if the server still lists it; otherwise pick one.
      setSelectedKey((current) => {
        if (current && loaded.models.some((m) => modelKey(m) === current)) return current
        const chosen = chooseModel(loaded, loadStoredChoice())
        return chosen ? modelKey(chosen) : null
      })
    } catch (caught) {
      setCatalogError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      setRefreshingModels(false)
    }
  }, [])

  const loadDetail = useCallback(async (id: string, signal?: AbortSignal) => {
    try {
      const loaded = await getConversation(id, signal)
      setDetail(loaded)
      setDetailError(null)
      scrollToEnd()
    } catch (caught) {
      if (signal?.aborted) return
      setDetailError({ id, error: caught instanceof Error ? caught : new Error(String(caught)) })
    }
  }, [scrollToEnd])

  useEffect(() => {
    let cancelled = false
    fetchChatOptions()
      .then((loaded) => !cancelled && setOptions(loaded))
      .catch(() => !cancelled && setOptions(null))
    // oxlint-disable-next-line react/set-state-in-effect -- loads data from the API
    void refreshList()
    void loadModels()
    return () => {
      cancelled = true
    }
  }, [refreshList, loadModels])

  useEffect(() => {
    if (!conversationId) return
    const controller = new AbortController()
    // oxlint-disable-next-line react/set-state-in-effect -- loads data from the API
    void loadDetail(conversationId, controller.signal)
    return () => controller.abort()
  }, [conversationId, loadDetail])

  // Stop a running answer when leaving the page.
  useEffect(() => () => abortRef.current?.abort(), [])

  const streaming = live !== null
  const maxChars = options?.max_question_chars ?? 4000
  const configured = options?.configured ?? true
  const selected: ModelEntry | null =
    catalog?.models.find((m) => selectedKey !== null && modelKey(m) === selectedKey) ?? null
  // Without the model list (it failed to load) questions go to the server's default model.
  const modelUsable = catalog === null || Boolean(selected?.selectable)
  const canAsk = configured && modelUsable

  function selectModel(model: ModelEntry) {
    setSelectedKey(modelKey(model))
    storeChoice(model)
    setSendError(null)
  }

  async function ask(question: string, clientMessageId: string) {
    setSendError(null)
    let id = conversationId
    if (!id) {
      try {
        id = (await createConversation()).id
      } catch (caught) {
        setSendError(caught instanceof Error ? caught.message : String(caught))
        setDraft(question)
        return
      }
      navigate(`/chat/${id}`)
    }
    const controller = new AbortController()
    abortRef.current = controller
    setLive({ conversationId: id, question, clientMessageId, userMessageId: null, answer: emptyAnswer() })
    setAnnouncement('Searching your documents and generating an answer.')
    let streamStarted = false
    try {
      await streamAnswer(
        id,
        {
          content: question,
          client_message_id: clientMessageId,
          ...(mode ? { retrieval: { mode } } : {}),
          ...(selected ? { model: { provider: selected.provider, model: selected.model } } : {}),
        },
        (event) => {
          streamStarted = true
          setLive((turn) => (turn ? applyEvent(turn, event) : turn))
          if (event.event === 'delta' || event.event === 'sources') scrollToEnd()
          if (event.event === 'done') setAnnouncement('Answer complete.')
          if (event.event === 'error') setAnnouncement(`The answer failed: ${event.data.message}`)
        },
        controller.signal,
      )
    } catch (caught) {
      if (controller.signal.aborted) {
        setAnnouncement('Answer stopped.')
      } else if (!streamStarted) {
        // Rejected before any answer started (busy, not configured, invalid): nothing was stored.
        setSendError(caught instanceof Error ? caught.message : String(caught))
        setDraft(question)
        setAnnouncement('')
        if (caught instanceof ApiError && MODEL_ERRORS.has(caught.code)) void loadModels(true)
      } else {
        setSendError('The connection was lost while the answer was streaming.')
      }
    } finally {
      abortRef.current = null
    }
    // Show the stored version (with citation status) in place of the live one.
    if (controller.signal.aborted) await new Promise((resolve) => setTimeout(resolve, 300))
    await Promise.all([loadDetail(id), refreshList()])
    setLive(null)
  }

  function onSubmit(event?: FormEvent) {
    event?.preventDefault()
    const question = draft.trim()
    if (!question || streaming || !canAsk) return
    if (question.length > maxChars) {
      setSendError(`The question must be at most ${maxChars} characters.`)
      return
    }
    setDraft('')
    void ask(question, newClientMessageId())
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter adds a line. Never send while an IME is composing (Japanese input).
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault()
      onSubmit()
    }
  }

  function retry(answer: ChatMessage) {
    const question = detail?.messages.find((m) => m.id === answer.reply_to_id)
    if (!question || streaming) return
    void ask(question.content, question.client_message_id ?? newClientMessageId())
  }

  async function confirmDelete() {
    if (!toDelete) return
    setDeleting(true)
    try {
      await deleteConversation(toDelete.id)
      if (toDelete.id === conversationId) navigate('/chat')
      setToDelete(null)
      await refreshList()
    } catch (caught) {
      setListError(caught instanceof Error ? caught.message : String(caught))
      setToDelete(null)
    } finally {
      setDeleting(false)
    }
  }

  async function rename(id: string, title: string) {
    await renameConversation(id, title)
    await refreshList()
    if (id === conversationId) await loadDetail(id)
  }

  const liveHere = live && live.conversationId === conversationId ? live : null
  const hidden = new Set([liveHere?.userMessageId, liveHere?.answer.id])
  const messages = (detail && detail.id === conversationId ? detail.messages : []).filter(
    (m) => !hidden.has(m.id) && !(liveHere && m.status === 'streaming'),
  )
  const currentError = detailError && detailError.id === conversationId ? detailError.error : null
  const notFound = currentError instanceof ApiError && currentError.status === 404

  const sidebar = (
    <ConversationSidebar
      conversations={conversations}
      error={listError}
      activeId={conversationId}
      busyId={deleting ? (toDelete?.id ?? null) : null}
      onNew={() => {
        setSidebarOpen(false)
        navigate('/chat')
      }}
      onRename={rename}
      onDelete={setToDelete}
      onNavigate={() => setSidebarOpen(false)}
    />
  )

  return (
    <div className="-mx-4 -my-8 flex h-[calc(100dvh-4rem)] min-h-[28rem] sm:-mx-6 lg:-mx-8">
      <aside className="hidden w-64 shrink-0 border-r border-slate-200 bg-white md:block">{sidebar}</aside>
      {sidebarOpen && (
        <div className="fixed inset-0 z-30 flex md:hidden">
          <button
            type="button"
            aria-label="Close conversations"
            className="absolute inset-0 bg-slate-900/40"
            onClick={() => setSidebarOpen(false)}
          />
          <div className="relative w-72 max-w-[85%] bg-white shadow-xl">{sidebar}</div>
        </div>
      )}

      <section aria-label="Chat" className="flex min-w-0 flex-1 flex-col bg-slate-50">
        <div className="flex items-center gap-2 border-b border-slate-200 bg-white px-4 py-2">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="rounded-md border border-slate-200 px-2 py-1 text-xs text-slate-700 md:hidden"
            aria-label="Show conversations"
          >
            Conversations
          </button>
          <h2 className="min-w-0 flex-1 truncate text-sm font-semibold text-slate-900">
            {conversationId ? (detail?.title ?? ' ') : 'New chat'}
          </h2>
          {/* Without the model list, the server's default model is the one that answers. */}
          {!catalog && options?.model && (
            <span className="hidden text-xs text-slate-500 sm:inline">
              Model: <span className="font-medium text-slate-700">{options.model}</span>
            </span>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
          {!configured && (
            <output className="mb-4 flex gap-2 rounded-md border border-amber-200 bg-amber-50 p-3 text-sm text-amber-900">
              <AlertIcon className="mt-0.5 h-4 w-4 shrink-0" />
              <p>
                Answer generation is not configured on the server: no language model provider is set up
                (ANTHROPIC_API_KEY, GEMINI_API_KEY, or OLLAMA_BASE_URL with OLLAMA_MODELS). You can still
                read and manage earlier conversations.
              </p>
            </output>
          )}
          {notFound && (
            <p role="alert" className="text-sm text-slate-600">
              This conversation does not exist. It may have been deleted.
            </p>
          )}
          {currentError && !notFound && (
            <p role="alert" className="text-sm text-red-700">
              The conversation could not be loaded: {currentError.message}
            </p>
          )}
          {!conversationId && !liveHere && (
            <div className="mx-auto mt-8 max-w-lg text-center">
              <ChatIcon className="mx-auto h-10 w-10 text-indigo-500" />
              <h3 className="mt-3 text-lg font-semibold text-slate-900">Ask your documents</h3>
              <p className="mt-2 text-sm text-slate-600">
                Questions in Japanese or English are answered from your uploaded documents only, with
                citations you can open. If the documents do not contain the answer, the assistant says so.
              </p>
              <ul className="mt-4 flex flex-wrap justify-center gap-2">
                {EXAMPLES.map((example) => (
                  <li key={example}>
                    <button
                      type="button"
                      onClick={() => setDraft(example)}
                      className="rounded-full border border-slate-200 bg-white px-3 py-1 text-xs text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-indigo-600"
                    >
                      {example}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ol className="mx-auto max-w-3xl space-y-4" aria-label="Messages">
            {messages.map((message) => (
              <li key={message.id}>
                {message.role === 'user' ? (
                  <UserBubble text={message.content} />
                ) : (
                  <AssistantMessage
                    message={storedAnswer(message)}
                    onRetry={
                      message.status === 'failed' || message.status === 'cancelled'
                        ? () => retry(message)
                        : undefined
                    }
                    retryDisabled={streaming || !canAsk}
                  />
                )}
              </li>
            ))}
            {liveHere && (
              <>
                <li>
                  <UserBubble text={liveHere.question} />
                </li>
                <li>
                  <AssistantMessage message={liveHere.answer} />
                </li>
              </>
            )}
          </ol>
          {detail?.messages_truncated && (
            <p className="mt-2 text-center text-xs text-slate-500">Only the latest messages are shown.</p>
          )}
          <div ref={endRef} />
        </div>

        <form onSubmit={onSubmit} className="border-t border-slate-200 bg-white px-4 py-3">
          <div className="sr-only" aria-live="polite">
            {announcement}
          </div>
          {sendError && (
            <p role="alert" className="mx-auto mb-2 max-w-3xl rounded-md bg-red-50 px-3 py-1.5 text-sm text-red-800">
              {sendError}
            </p>
          )}
          <div className="mx-auto flex max-w-3xl flex-col gap-2">
            {catalog && configured && (
              <ModelSelector
                catalog={catalog}
                selected={selected}
                onSelect={selectModel}
                onRefresh={() => void loadModels(true)}
                refreshing={refreshingModels}
                disabled={streaming}
              />
            )}
            {catalogError && configured && (
              <p className="text-xs text-slate-500">
                The model list could not be loaded ({catalogError}); questions use the server&apos;s
                default model{options?.model ? ` (${options.model})` : ''}.
              </p>
            )}
            <label htmlFor={inputId} className="sr-only">
              Your question
            </label>
            <textarea
              id={inputId}
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={onKeyDown}
              rows={2}
              disabled={!configured}
              placeholder={configured ? 'Ask a question about your documents…' : 'Answer generation is not configured.'}
              aria-describedby={`${inputId}-hint`}
              className="w-full resize-y rounded-lg border border-slate-300 px-3 py-2 text-sm shadow-sm focus-visible:outline-2 focus-visible:outline-indigo-600 disabled:bg-slate-100"
            />
            <div className="flex flex-wrap items-center gap-2 text-xs text-slate-500">
              <label htmlFor={modeId}>Retrieval</label>
              <select
                id={modeId}
                value={mode}
                onChange={(event) => setMode(event.target.value as SearchMode | '')}
                className="rounded border border-slate-300 bg-white px-1.5 py-1 text-xs"
              >
                <option value="">
                  Default{options ? ` (${MODE_LABELS[options.default_retrieval_mode]})` : ''}
                </option>
                {(options?.retrieval_modes ?? (['semantic', 'hybrid', 'keyword'] as SearchMode[])).map((value) => (
                  <option key={value} value={value}>
                    {MODE_LABELS[value]}
                  </option>
                ))}
              </select>
              <span id={`${inputId}-hint`} className="flex-1">
                Enter to send, Shift+Enter for a new line · {draft.length}/{maxChars}
              </span>
              {streaming ? (
                <button
                  type="button"
                  onClick={() => abortRef.current?.abort()}
                  className="rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-indigo-600"
                >
                  Stop
                </button>
              ) : (
                <button
                  type="submit"
                  disabled={!canAsk || !draft.trim()}
                  className="rounded-md bg-indigo-600 px-3 py-1.5 text-sm font-semibold text-white shadow-sm hover:bg-indigo-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:opacity-50"
                >
                  Send
                </button>
              )}
            </div>
            <p className="text-[11px] text-slate-400">
              This application has no sign-in: anyone who can reach this server can read these
              conversations. Answers can be wrong; check the cited passages.
            </p>
          </div>
        </form>
      </section>

      <ConfirmDialog
        open={toDelete !== null}
        title="Delete conversation?"
        confirmLabel="Delete"
        busy={deleting}
        onConfirm={confirmDelete}
        onCancel={() => setToDelete(null)}
      >
        “{toDelete?.title}” and all of its messages will be permanently deleted.
      </ConfirmDialog>
    </div>
  )
}
