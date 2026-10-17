import { useId, useState, type FormEvent } from 'react'

import { ApiError, type ConnectorCreate, type ConnectorOptions } from '../../lib/api'
import {
  INTERVAL_PRESETS,
  intervalLabel,
  normalizePrefix,
  sourceAllowed,
  validateBucket,
  validateInterval,
  validateName,
  validatePrefix,
} from '../../lib/connectors'

const inputClass =
  'mt-1 block w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus-visible:outline-2 focus-visible:outline-indigo-600 disabled:bg-slate-50 disabled:text-slate-500 aria-[invalid=true]:border-rose-400'

export interface ConnectorFormValues {
  name: string
  bucket: string
  prefix: string
  sync_interval_minutes: number
  enabled: boolean
}

type Field = 'name' | 'bucket' | 'prefix' | 'sync_interval_minutes'

interface ConnectorFormProps {
  mode: 'create' | 'edit'
  options: ConnectorOptions
  initial?: ConnectorFormValues
  onSubmit: (values: ConnectorCreate) => Promise<void>
  onCancel: () => void
}

const EMPTY: ConnectorFormValues = {
  name: '',
  bucket: '',
  prefix: '',
  sync_interval_minutes: 60,
  enabled: true,
}

/**
 * Create or edit a connector. Only the bucket and prefix are entered here; AWS credentials
 * (and any S3-compatible endpoint) are configured on the backend and never in the browser.
 * Bucket and prefix cannot change after creation.
 */
export default function ConnectorForm({
  mode,
  options,
  initial = EMPTY,
  onSubmit,
  onCancel,
}: ConnectorFormProps) {
  const id = useId()
  const [values, setValues] = useState<ConnectorFormValues>(initial)
  const [errors, setErrors] = useState<Partial<Record<Field, string>>>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const intervals = INTERVAL_PRESETS.some((p) => p.minutes === values.sync_interval_minutes)
    ? INTERVAL_PRESETS
    : [
        ...INTERVAL_PRESETS,
        {
          minutes: values.sync_interval_minutes,
          label: intervalLabel(values.sync_interval_minutes),
        },
      ]

  function validate(): Partial<Record<Field, string>> {
    const found: Partial<Record<Field, string>> = {}
    const name = validateName(values.name)
    if (name) found.name = name
    const interval = validateInterval(
      values.sync_interval_minutes,
      options.min_interval_minutes,
      options.max_interval_minutes,
    )
    if (interval) found.sync_interval_minutes = interval
    if (mode === 'create') {
      const bucket = validateBucket(values.bucket)
      const prefix = validatePrefix(values.prefix)
      if (bucket) found.bucket = bucket
      if (prefix) found.prefix = prefix
      if (!bucket && !prefix && !sourceAllowed(options.allowed_sources, values.bucket, values.prefix)) {
        found.bucket = 'This bucket and prefix are not in the server’s allowed sources.'
      }
    }
    return found
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    const found = validate()
    setErrors(found)
    setSubmitError(null)
    if (Object.keys(found).length > 0) return
    setSubmitting(true)
    try {
      await onSubmit({
        name: values.name.trim(),
        bucket: values.bucket.trim(),
        prefix: normalizePrefix(values.prefix),
        sync_interval_minutes: values.sync_interval_minutes,
        enabled: values.enabled,
      })
    } catch (error) {
      if (error instanceof ApiError) {
        const field: Field | null =
          error.code === 'invalid_bucket' || error.code === 'source_not_allowed'
            ? 'bucket'
            : error.code === 'invalid_prefix'
              ? 'prefix'
              : error.code === 'invalid_name' || error.code === 'connector_name_taken'
                ? 'name'
                : null
        if (field) setErrors({ [field]: error.message })
        else setSubmitError(error.message)
      } else {
        setSubmitError('The connector could not be saved.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  function fieldProps(field: Field) {
    return {
      id: `${id}-${field}`,
      'aria-invalid': errors[field] ? true : undefined,
      'aria-describedby': `${id}-${field}-hint${errors[field] ? ` ${id}-${field}-error` : ''}`,
    }
  }

  function fieldError(field: Field) {
    if (!errors[field]) return null
    return (
      <p id={`${id}-${field}-error`} className="mt-1 text-xs text-rose-700">
        {errors[field]}
      </p>
    )
  }

  const locked = mode === 'edit'

  return (
    <form onSubmit={(event) => void handleSubmit(event)} noValidate className="space-y-4">
      <div className="rounded-lg bg-slate-50 p-3 text-xs leading-5 text-slate-600">
        AWS credentials are configured on the backend (environment variables, an AWS profile or
        an IAM role) and are never entered or shown here. Only buckets listed in the server’s{' '}
        <code className="font-mono">S3_ALLOWED_SOURCES</code> can be used:{' '}
        <span className="font-medium text-slate-800">{options.allowed_sources.join(', ')}</span>.
      </div>

      <div>
        <label htmlFor={`${id}-name`} className="block text-sm font-medium text-slate-800">
          Name
        </label>
        <input
          {...fieldProps('name')}
          className={inputClass}
          value={values.name}
          maxLength={100}
          onChange={(event) => setValues({ ...values, name: event.target.value })}
          placeholder="Team handbook"
        />
        <p id={`${id}-name-hint`} className="sr-only">
          A name for this connector.
        </p>
        {fieldError('name')}
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor={`${id}-bucket`} className="block text-sm font-medium text-slate-800">
            S3 bucket
          </label>
          <input
            {...fieldProps('bucket')}
            className={`${inputClass} font-mono`}
            value={values.bucket}
            disabled={locked}
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            onChange={(event) => setValues({ ...values, bucket: event.target.value })}
            placeholder="team-docs"
          />
          <p id={`${id}-bucket-hint`} className="mt-1 text-xs text-slate-500">
            {locked ? 'Cannot be changed after creation.' : 'The bucket name only, without s3://.'}
          </p>
          {fieldError('bucket')}
        </div>
        <div>
          <label htmlFor={`${id}-prefix`} className="block text-sm font-medium text-slate-800">
            Prefix (folder) <span className="font-normal text-slate-500">optional</span>
          </label>
          <input
            {...fieldProps('prefix')}
            className={`${inputClass} font-mono`}
            value={values.prefix}
            disabled={locked}
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            onChange={(event) => setValues({ ...values, prefix: event.target.value })}
            placeholder="handbook/"
          />
          <p id={`${id}-prefix-hint`} className="mt-1 text-xs text-slate-500">
            {locked
              ? 'Cannot be changed after creation.'
              : `Leave empty for the whole bucket. Imports ${options.supported_extensions.join(', ')} files.`}
          </p>
          {fieldError('prefix')}
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2">
        <div>
          <label htmlFor={`${id}-sync_interval_minutes`} className="block text-sm font-medium text-slate-800">
            Automatic sync
          </label>
          <select
            {...fieldProps('sync_interval_minutes')}
            className={inputClass}
            value={values.sync_interval_minutes}
            onChange={(event) =>
              setValues({ ...values, sync_interval_minutes: Number(event.target.value) })
            }
          >
            {intervals.map((preset) => (
              <option key={preset.minutes} value={preset.minutes}>
                {preset.label}
              </option>
            ))}
          </select>
          <p id={`${id}-sync_interval_minutes-hint`} className="mt-1 text-xs text-slate-500">
            Run by the backend worker; the browser does not need to stay open.
          </p>
          {fieldError('sync_interval_minutes')}
        </div>
        <div className="flex items-start gap-3 sm:pt-7">
          <input
            id={`${id}-enabled`}
            type="checkbox"
            checked={values.enabled}
            onChange={(event) => setValues({ ...values, enabled: event.target.checked })}
            className="mt-0.5 h-4 w-4 rounded border-slate-300 text-indigo-600 focus-visible:outline-2 focus-visible:outline-indigo-600"
          />
          <label htmlFor={`${id}-enabled`} className="text-sm text-slate-700">
            <span className="font-medium text-slate-800">Enable automatic syncs</span>
            <span className="block text-xs text-slate-500">
              Manual syncs work either way.
            </span>
          </label>
        </div>
      </div>

      {submitError && (
        <p role="alert" className="rounded-md bg-rose-50 p-3 text-sm text-rose-800">
          {submitError}
        </p>
      )}

      <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="rounded-lg border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:opacity-50"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={submitting}
          className="rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:opacity-50"
        >
          {submitting ? 'Saving…' : mode === 'create' ? 'Create connector' : 'Save changes'}
        </button>
      </div>
    </form>
  )
}
