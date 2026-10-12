import type { DocumentStatus, FileType, Language } from './api'

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value >= 10 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return '—'
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

export const LANGUAGE_LABELS: Record<Language, string> = {
  ja: 'Japanese',
  en: 'English',
  mixed: 'Mixed (JA/EN)',
  unknown: 'Unknown',
}

export function languageLabel(language: Language | null): string {
  return language ? LANGUAGE_LABELS[language] : '—'
}

export const FILE_TYPE_LABELS: Record<FileType, string> = {
  pdf: 'PDF',
  markdown: 'Markdown',
  text: 'Text',
}

export const STATUS_LABELS: Record<DocumentStatus, string> = {
  pending: 'Queued',
  processing: 'Processing',
  completed: 'Indexed',
  failed: 'Failed',
}

export function isActive(status: DocumentStatus): boolean {
  return status === 'pending' || status === 'processing'
}

export function pageRange(start: number | null, end: number | null): string | null {
  if (start === null) return null
  return end === null || end === start ? `p. ${start}` : `pp. ${start}–${end}`
}
