import { describe, expect, it } from 'vitest'

import { file, LIMITS } from '../test/fixtures'
import { validateUploadFile } from './validation'

describe('validateUploadFile', () => {
  it.each(['report.pdf', 'NOTES.MD', 'readme.markdown', '日本語.txt'])('accepts %s', (name) => {
    expect(validateUploadFile(file(name, 1000), LIMITS)).toBeNull()
  })

  it.each(['photo.png', 'doc.docx', 'archive.pdf.zip', 'noextension'])('rejects %s', (name) => {
    expect(validateUploadFile(file(name, 1000), LIMITS)).toMatch(/unsupported file type/)
  })

  it('rejects empty files', () => {
    expect(validateUploadFile(file('a.txt', 0), LIMITS)).toMatch(/empty/)
  })

  it('rejects files over the limit and names both sizes', () => {
    const message = validateUploadFile(file('big.pdf', 21 * 1024 * 1024), LIMITS)
    expect(message).toMatch(/21 MB exceeds the 20 MB limit/)
  })

  it('accepts a file exactly at the limit', () => {
    expect(validateUploadFile(file('max.pdf', LIMITS.max_upload_bytes), LIMITS)).toBeNull()
  })
})
