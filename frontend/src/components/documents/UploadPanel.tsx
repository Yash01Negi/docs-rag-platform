import { useId, useRef, useState, type DragEvent } from 'react'
import { Link } from 'react-router'

import { ApiError, uploadDocument, type UploadLimits, type UploadResult } from '../../lib/api'
import { formatBytes } from '../../lib/format'
import { validateUploadFile } from '../../lib/validation'
import { UploadIcon } from '../icons'

type UploadState =
  | { kind: 'idle' }
  | { kind: 'uploading'; filename: string; progress: number | null }
  | { kind: 'done'; filename: string; result: UploadResult }
  | { kind: 'error'; message: string }

interface UploadPanelProps {
  limits: UploadLimits | null
  limitsError: string | null
  onUploaded: (result: UploadResult) => void
}

export default function UploadPanel({ limits, limitsError, onUploaded }: UploadPanelProps) {
  const [state, setState] = useState<UploadState>({ kind: 'idle' })
  const [dragging, setDragging] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const helpId = useId()
  const busy = state.kind === 'uploading'
  const disabled = busy || limits === null

  async function handleFile(file: File) {
    if (!limits) return
    const problem = validateUploadFile(file, limits)
    if (problem) {
      setState({ kind: 'error', message: problem })
      return
    }
    setState({ kind: 'uploading', filename: file.name, progress: 0 })
    try {
      const result = await uploadDocument(file, (progress) =>
        setState({ kind: 'uploading', filename: file.name, progress }),
      )
      setState({ kind: 'done', filename: file.name, result })
      onUploaded(result)
    } catch (error) {
      const message =
        error instanceof ApiError ? error.message : 'The upload failed for an unknown reason.'
      setState({ kind: 'error', message: `${file.name}: ${message}` })
    }
  }

  function handleFiles(files: FileList | null) {
    if (!files || files.length === 0) return
    if (files.length > 1) {
      setState({ kind: 'error', message: 'Please upload one file at a time.' })
      return
    }
    void handleFile(files[0])
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault()
    setDragging(false)
    if (!disabled) handleFiles(event.dataTransfer.files)
  }

  const accept = limits?.accepted_extensions.join(',')

  return (
    <section
      aria-labelledby="upload-heading"
      className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm"
    >
      <h2 id="upload-heading" className="text-sm font-semibold text-slate-900">
        Upload a document
      </h2>

      <div
        onDragOver={(event) => {
          event.preventDefault()
          if (!disabled) setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        data-testid="dropzone"
        className="mt-3"
      >
        <button
          type="button"
          disabled={disabled}
          aria-describedby={helpId}
          onClick={() => inputRef.current?.click()}
          className={`flex w-full flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-8 text-center transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-60 ${
            dragging
              ? 'border-indigo-400 bg-indigo-50'
              : 'border-slate-300 bg-slate-50 hover:border-indigo-300 hover:bg-indigo-50/40'
          }`}
        >
          <UploadIcon className="h-8 w-8 text-slate-400" />
          <span className="mt-2 text-sm font-medium text-slate-800">
            Drop a file here or <span className="text-indigo-700 underline">browse</span>
          </span>
          <span id={helpId} className="mt-1 text-xs text-slate-500">
            {limits
              ? `PDF, Markdown or plain text (${limits.accepted_extensions.join(', ')}), up to ${formatBytes(limits.max_upload_bytes)}. Japanese and English.`
              : (limitsError ?? 'Loading upload limits…')}
          </span>
        </button>
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          className="sr-only"
          tabIndex={-1}
          aria-label="File to upload"
          data-testid="file-input"
          onChange={(event) => {
            handleFiles(event.target.files)
            event.target.value = '' // allow choosing the same file again
          }}
        />
      </div>

      <div aria-live="polite" className="mt-3 min-h-6 text-sm">
        {state.kind === 'uploading' && (
          <div>
            <p className="text-slate-700">
              Uploading <span className="font-medium">{state.filename}</span>
              {state.progress !== null && ` (${Math.round(state.progress * 100)}%)`}
            </p>
            <progress
              className="mt-2 h-1.5 w-full overflow-hidden rounded-full [&::-moz-progress-bar]:bg-indigo-600 [&::-webkit-progress-bar]:bg-slate-200 [&::-webkit-progress-value]:bg-indigo-600"
              max={1}
              value={state.progress ?? undefined}
              aria-label="Upload progress"
            />
          </div>
        )}
        {state.kind === 'done' && (
          <p className={state.result.duplicate ? 'text-amber-800' : 'text-emerald-800'}>
            {state.result.duplicate ? (
              <>
                This content was already uploaded as{' '}
                <Link
                  className="font-medium underline"
                  to={`/documents/${state.result.document.id}`}
                >
                  {state.result.document.filename}
                </Link>
                . No duplicate was created.
              </>
            ) : (
              <>
                <span className="font-medium">{state.filename}</span> uploaded and queued for
                indexing.
              </>
            )}
          </p>
        )}
        {state.kind === 'error' && (
          <p role="alert" className="text-rose-700">
            {state.message}
          </p>
        )}
      </div>
    </section>
  )
}
