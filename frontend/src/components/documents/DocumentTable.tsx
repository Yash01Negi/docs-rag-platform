import { Link } from 'react-router'

import type { DocumentSummary } from '../../lib/api'
import { FILE_TYPE_LABELS, formatBytes, formatDateTime, languageLabel, pluralize } from '../../lib/format'
import DocumentStatusBadge from './DocumentStatusBadge'

const linkClass =
  'font-medium text-slate-900 hover:text-indigo-700 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 rounded-sm break-all'

function ErrorSummary({ document }: { document: DocumentSummary }) {
  if (!document.error_message) return null
  return (
    <p className="mt-1 line-clamp-2 text-xs text-rose-700" title={document.error_message}>
      {document.error_message}
    </p>
  )
}

/** Document library: a table on wide screens and a card list on narrow ones. */
export default function DocumentTable({ documents }: { documents: DocumentSummary[] }) {
  return (
    <>
      <div className="hidden overflow-x-auto md:block">
        <table className="min-w-full divide-y divide-slate-200 text-sm">
          <caption className="sr-only">Uploaded documents</caption>
          <thead className="bg-slate-50 text-left text-xs font-semibold tracking-wide text-slate-500 uppercase">
            <tr>
              <th scope="col" className="px-4 py-3">
                Name
              </th>
              <th scope="col" className="px-4 py-3">
                Type
              </th>
              <th scope="col" className="px-4 py-3 text-right">
                Size
              </th>
              <th scope="col" className="px-4 py-3">
                Language
              </th>
              <th scope="col" className="px-4 py-3">
                Status
              </th>
              <th scope="col" className="px-4 py-3 text-right">
                Chunks
              </th>
              <th scope="col" className="px-4 py-3">
                Uploaded
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100 bg-white">
            {documents.map((document) => (
              <tr key={document.id} className="align-top hover:bg-slate-50">
                <td className="max-w-xs px-4 py-3">
                  <Link to={`/documents/${document.id}`} className={linkClass}>
                    {document.filename}
                  </Link>
                  <ErrorSummary document={document} />
                </td>
                <td className="px-4 py-3 text-slate-600">{FILE_TYPE_LABELS[document.file_type]}</td>
                <td className="px-4 py-3 text-right whitespace-nowrap text-slate-600 tabular-nums">
                  {formatBytes(document.size_bytes)}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-slate-600">
                  {languageLabel(document.language)}
                </td>
                <td className="px-4 py-3">
                  <DocumentStatusBadge status={document.status} />
                </td>
                <td className="px-4 py-3 text-right text-slate-600 tabular-nums">
                  {document.chunk_count}
                </td>
                <td className="px-4 py-3 whitespace-nowrap text-slate-600">
                  {formatDateTime(document.created_at)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <ul className="divide-y divide-slate-100 md:hidden" aria-label="Uploaded documents">
        {documents.map((document) => (
          <li key={document.id} className="px-4 py-4">
            <div className="flex items-start justify-between gap-3">
              <Link to={`/documents/${document.id}`} className={linkClass}>
                {document.filename}
              </Link>
              <DocumentStatusBadge status={document.status} />
            </div>
            <p className="mt-1 text-xs text-slate-500">
              {FILE_TYPE_LABELS[document.file_type]} · {formatBytes(document.size_bytes)} ·{' '}
              {languageLabel(document.language)} · {pluralize(document.chunk_count, 'chunk')}
            </p>
            <p className="mt-0.5 text-xs text-slate-500">{formatDateTime(document.created_at)}</p>
            <ErrorSummary document={document} />
          </li>
        ))}
      </ul>
    </>
  )
}
