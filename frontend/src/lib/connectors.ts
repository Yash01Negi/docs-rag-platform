// Connector form validation and display helpers. The backend validates everything again;
// these checks only give quicker, clearer feedback.

import type { Connector, ConnectorDetail, SyncRun, SyncRunStatus } from './api'

export const SYNC_STATUS_LABELS: Record<SyncRunStatus, string> = {
  pending: 'Queued',
  running: 'Syncing',
  succeeded: 'Synced',
  partial: 'Synced with errors',
  failed: 'Failed',
  cancelled: 'Cancelled',
}

export function isSyncActive(status: SyncRunStatus | null | undefined): boolean {
  return status === 'pending' || status === 'running'
}

/** Poll a connector list only while one of its connectors is syncing. */
export function anySyncActive(connectors: Connector[]): boolean {
  return connectors.some((connector) => isSyncActive(connector.last_sync_status))
}

export function hasConnectorError(connector: Connector): boolean {
  return (
    connector.last_sync_status === 'failed' ||
    connector.last_sync_status === 'partial' ||
    (connector.last_error_message !== null && !isSyncActive(connector.last_sync_status))
  )
}

export const INTERVAL_PRESETS: { minutes: number; label: string }[] = [
  { minutes: 15, label: 'Every 15 minutes' },
  { minutes: 60, label: 'Every hour' },
  { minutes: 360, label: 'Every 6 hours' },
  { minutes: 1440, label: 'Every day' },
  { minutes: 10080, label: 'Every week' },
]

export function intervalLabel(minutes: number): string {
  const preset = INTERVAL_PRESETS.find((item) => item.minutes === minutes)
  if (preset) return preset.label
  if (minutes % 1440 === 0) return `Every ${minutes / 1440} days`
  if (minutes % 60 === 0) return `Every ${minutes / 60} hours`
  return `Every ${minutes} minutes`
}

const BUCKET = /^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$/
const IPV4 = /^\d{1,3}(\.\d{1,3}){3}$/
// eslint-disable-next-line no-control-regex
const CONTROL = /[\u0000-\u001f\u007f]/

export function validateBucket(value: string): string | null {
  const bucket = value.trim()
  if (!bucket) return 'Enter a bucket name.'
  if (bucket.startsWith('s3://') || bucket.includes('/')) {
    return 'Enter only the bucket name; put any folder in Prefix.'
  }
  if (!BUCKET.test(bucket)) {
    return 'Use 3–63 lowercase letters, digits, dots or hyphens, starting and ending with a letter or digit.'
  }
  if (bucket.includes('..') || bucket.includes('.-') || bucket.includes('-.')) {
    return "Bucket names cannot contain '..', '.-' or '-.'."
  }
  if (IPV4.test(bucket)) return 'Bucket names cannot look like IP addresses.'
  return null
}

/** The prefix as the backend stores it: '' or a folder ending in '/'. */
export function normalizePrefix(value: string): string {
  const prefix = value.trim()
  if (!prefix) return ''
  return prefix.endsWith('/') ? prefix : `${prefix}/`
}

export function validatePrefix(value: string): string | null {
  const prefix = normalizePrefix(value)
  if (!prefix) return null
  if (prefix.startsWith('/')) return "The prefix must not start with '/'."
  if (prefix.includes('\\') || CONTROL.test(prefix)) {
    return 'The prefix contains characters that are not allowed.'
  }
  if (prefix.length > 512) return 'The prefix is longer than 512 characters.'
  const segments = prefix.slice(0, -1).split('/')
  if (segments.some((segment) => segment === '' || segment === '.' || segment === '..')) {
    return "The prefix must not contain empty, '.' or '..' folders."
  }
  return null
}

export function validateName(value: string): string | null {
  const name = value.trim()
  if (!name) return 'Enter a name.'
  if (name.length > 100) return 'Use at most 100 characters.'
  return null
}

export function validateInterval(minutes: number, min = 5, max = 10080): string | null {
  if (!Number.isInteger(minutes) || minutes < min || minutes > max) {
    return `Choose between ${min} minutes and ${max / 1440} days.`
  }
  return null
}

/** Whether an allowlist entry ("bucket" or "bucket/prefix/") covers this bucket and prefix. */
export function sourceAllowed(allowed: string[], bucket: string, prefix: string): boolean {
  const normalized = normalizePrefix(prefix)
  return allowed.some((entry) => {
    const slash = entry.indexOf('/')
    const entryBucket = slash === -1 ? entry : entry.slice(0, slash)
    const scope = slash === -1 ? '' : normalizePrefix(entry.slice(slash + 1))
    return entryBucket === bucket.trim() && normalized.startsWith(scope)
  })
}

export function runCounts(run: SyncRun): { label: string; value: number }[] {
  return [
    { label: 'Imported', value: run.documents_imported },
    { label: 'Updated', value: run.documents_updated },
    { label: 'Unchanged', value: run.documents_unchanged },
    { label: 'Removed', value: run.documents_removed },
    { label: 'Skipped', value: run.objects_skipped },
    { label: 'Failed', value: run.objects_failed },
  ]
}

export function runDuration(run: SyncRun): string | null {
  if (!run.started_at || !run.finished_at) return null
  const seconds = Math.max(0, (Date.parse(run.finished_at) - Date.parse(run.started_at)) / 1000)
  if (seconds < 1) return '<1 s'
  if (seconds < 60) return `${Math.round(seconds)} s`
  return `${Math.floor(seconds / 60)} min ${Math.round(seconds % 60)} s`
}

export function shouldPollConnector(connector: ConnectorDetail): boolean {
  return connector.active_run !== null
}
