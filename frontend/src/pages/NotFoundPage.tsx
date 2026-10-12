import { Link } from 'react-router'

export default function NotFoundPage() {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-10 text-center shadow-sm">
      <p className="text-sm font-semibold text-indigo-600">404</p>
      <h2 className="mt-2 text-xl font-semibold text-slate-900">Page not found</h2>
      <p className="mt-2 text-sm text-slate-600">The page you are looking for does not exist.</p>
      <Link
        to="/"
        className="mt-6 inline-flex rounded-lg bg-indigo-600 px-4 py-2 text-sm font-medium text-white hover:bg-indigo-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600"
      >
        Back to dashboard
      </Link>
    </section>
  )
}
