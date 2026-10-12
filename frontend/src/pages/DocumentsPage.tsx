import ComingSoon from '../components/ComingSoon'
import { DocumentsIcon } from '../components/icons'

export default function DocumentsPage() {
  return (
    <ComingSoon
      icon={DocumentsIcon}
      title="Documents"
      description="Manage the Japanese and English documents that YashRAG searches over."
      planned={[
        'Upload PDF, Markdown and text documents',
        'Language-aware parsing and chunking (Japanese and English)',
        'Embedding generation stored in PostgreSQL with pgvector',
        'Ingestion status and per-document metadata',
      ]}
    />
  )
}
