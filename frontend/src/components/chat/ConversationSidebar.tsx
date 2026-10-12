import { useState, type FormEvent, type KeyboardEvent } from 'react'
import { Link } from 'react-router'

import type { ConversationSummary } from '../../lib/chat'
import { formatDateTime } from '../../lib/format'

interface Props {
  conversations: ConversationSummary[] | null
  error: string | null
  activeId: string | undefined
  busyId: string | null
  onNew: () => void
  onRename: (id: string, title: string) => Promise<void>
  onDelete: (conversation: ConversationSummary) => void
  onNavigate?: () => void
}

export default function ConversationSidebar({
  conversations,
  error,
  activeId,
  busyId,
  onNew,
  onRename,
  onDelete,
  onNavigate,
}: Props) {
  const [editing, setEditing] = useState<string | null>(null)
  const [title, setTitle] = useState('')
  const [renameError, setRenameError] = useState<string | null>(null)

  function startEditing(conversation: ConversationSummary) {
    setEditing(conversation.id)
    setTitle(conversation.title)
    setRenameError(null)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!editing) return
    const trimmed = title.trim()
    if (!trimmed) {
      setRenameError('The title cannot be empty.')
      return
    }
    try {
      await onRename(editing, trimmed)
      setEditing(null)
    } catch (caught) {
      setRenameError(caught instanceof Error ? caught.message : String(caught))
    }
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === 'Escape') {
      event.preventDefault()
      setEditing(null)
    }
  }

  return (
    <nav aria-label="Conversations" className="flex h-full min-h-0 flex-col">
      <button
        type="button"
        onClick={onNew}
        className="m-3 rounded-md bg-indigo-600 px-3 py-2 text-sm font-semibold text-white shadow-sm hover:bg-indigo-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600"
      >
        New chat
      </button>
      {error && (
        <p role="alert" className="mx-3 rounded-md bg-red-50 px-2 py-1 text-xs text-red-800">
          {error}
        </p>
      )}
      {conversations === null && !error && (
        <output className="px-4 py-2 text-sm text-slate-500">Loading conversations…</output>
      )}
      {conversations?.length === 0 && (
        <p className="px-4 py-2 text-sm text-slate-500">No conversations yet.</p>
      )}
      <ul className="min-h-0 flex-1 space-y-0.5 overflow-y-auto px-2 pb-3">
        {conversations?.map((conversation) => {
          const active = conversation.id === activeId
          if (editing === conversation.id) {
            return (
              <li key={conversation.id} className="px-1 py-1">
                <form onSubmit={submit}>
                  <label htmlFor={`rename-${conversation.id}`} className="sr-only">
                    Conversation title
                  </label>
                  <input
                    id={`rename-${conversation.id}`}
                    value={title}
                    maxLength={200}
                    // oxlint-disable-next-line jsx-a11y/no-autofocus -- focus follows the Rename action
                    autoFocus
                    onChange={(event) => setTitle(event.target.value)}
                    onKeyDown={onKeyDown}
                    className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm focus-visible:outline-2 focus-visible:outline-indigo-600"
                  />
                  {renameError && <p className="mt-1 text-xs text-red-700">{renameError}</p>}
                  <div className="mt-1 flex gap-1 text-xs">
                    <button type="submit" className="rounded bg-indigo-600 px-2 py-0.5 text-white">
                      Save
                    </button>
                    <button
                      type="button"
                      onClick={() => setEditing(null)}
                      className="rounded px-2 py-0.5 text-slate-600 hover:bg-slate-100"
                    >
                      Cancel
                    </button>
                  </div>
                </form>
              </li>
            )
          }
          return (
            <li key={conversation.id} className="group relative">
              <Link
                to={`/chat/${conversation.id}`}
                onClick={onNavigate}
                aria-current={active ? 'page' : undefined}
                className={`block rounded-md px-3 py-2 pr-16 text-sm focus-visible:outline-2 focus-visible:outline-indigo-600 ${
                  active ? 'bg-indigo-50 text-indigo-800' : 'text-slate-700 hover:bg-slate-100'
                }`}
              >
                <span className="block truncate font-medium" title={conversation.title}>
                  {conversation.title}
                </span>
                <span className="block text-xs text-slate-500">
                  {formatDateTime(conversation.updated_at)}
                </span>
              </Link>
              <div className="absolute top-1.5 right-1 flex gap-0.5 opacity-100 sm:opacity-0 sm:group-focus-within:opacity-100 sm:group-hover:opacity-100">
                <button
                  type="button"
                  onClick={() => startEditing(conversation)}
                  className="rounded px-1.5 py-0.5 text-xs text-slate-600 hover:bg-white focus-visible:outline-2 focus-visible:outline-indigo-600"
                  aria-label={`Rename ${conversation.title}`}
                >
                  Rename
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(conversation)}
                  disabled={busyId === conversation.id}
                  className="rounded px-1.5 py-0.5 text-xs text-red-700 hover:bg-white focus-visible:outline-2 focus-visible:outline-indigo-600 disabled:opacity-50"
                  aria-label={`Delete ${conversation.title}`}
                >
                  Delete
                </button>
              </div>
            </li>
          )
        })}
      </ul>
    </nav>
  )
}
