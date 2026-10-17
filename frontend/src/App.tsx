import { Route, Routes } from 'react-router'

import AppLayout from './components/AppLayout'
import ChatPage from './pages/ChatPage'
import DashboardPage from './pages/DashboardPage'
import DocumentDetailPage from './pages/DocumentDetailPage'
import DocumentsPage from './pages/DocumentsPage'
import EvaluationPage from './pages/EvaluationPage'
import NotFoundPage from './pages/NotFoundPage'
import SearchPage from './pages/SearchPage'

export default function App() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<DashboardPage />} />
        <Route path="documents" element={<DocumentsPage />} />
        <Route path="documents/:documentId" element={<DocumentDetailPage />} />
        <Route path="search" element={<SearchPage />} />
        <Route path="chat" element={<ChatPage />} />
        <Route path="chat/:conversationId" element={<ChatPage />} />
        <Route path="evaluation" element={<EvaluationPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  )
}
