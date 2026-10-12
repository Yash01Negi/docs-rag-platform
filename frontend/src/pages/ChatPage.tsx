import ComingSoon from '../components/ComingSoon'
import { ChatIcon } from '../components/icons'

export default function ChatPage() {
  return (
    <ComingSoon
      icon={ChatIcon}
      title="Chat"
      description="Ask questions about your documents in Japanese or English and get answers grounded in them."
      planned={[
        'Hybrid retrieval: vector similarity plus keyword search',
        'Reranking of retrieved passages',
        'Answers with citations linking back to source passages',
        'Cross-lingual questions (ask in English about Japanese documents, and vice versa)',
      ]}
    />
  )
}
