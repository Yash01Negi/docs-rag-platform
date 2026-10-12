import type { UploadLimits } from './api'
import { formatBytes } from './format'

/**
 * Client-side checks for quick feedback. The server validates the content again and is the
 * authority: these checks only look at the name and size.
 */
export function validateUploadFile(file: File, limits: UploadLimits): string | null {
  const name = file.name.toLowerCase()
  const dot = name.lastIndexOf('.')
  const extension = dot >= 0 ? name.slice(dot) : ''
  if (!limits.accepted_extensions.includes(extension)) {
    return `${file.name}: unsupported file type. Accepted: ${limits.accepted_extensions.join(', ')}.`
  }
  if (file.size === 0) {
    return `${file.name}: the file is empty.`
  }
  if (file.size > limits.max_upload_bytes) {
    return `${file.name}: ${formatBytes(file.size)} exceeds the ${formatBytes(limits.max_upload_bytes)} limit.`
  }
  return null
}
