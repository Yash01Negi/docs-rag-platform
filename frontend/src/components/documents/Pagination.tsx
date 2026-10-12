interface PaginationProps {
  total: number
  limit: number
  offset: number
  onChange: (offset: number) => void
  label: string
}

const buttonClass =
  'rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-600 disabled:cursor-not-allowed disabled:opacity-50'

export default function Pagination({ total, limit, offset, onChange, label }: PaginationProps) {
  if (total <= limit) return null
  const first = offset + 1
  const last = Math.min(offset + limit, total)
  return (
    <nav aria-label={label} className="flex items-center justify-between gap-3 text-sm">
      <p className="text-slate-600">
        {first}–{last} of {total}
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          className={buttonClass}
          onClick={() => onChange(Math.max(offset - limit, 0))}
          disabled={offset === 0}
        >
          Previous
        </button>
        <button
          type="button"
          className={buttonClass}
          onClick={() => onChange(offset + limit)}
          disabled={last >= total}
        >
          Next
        </button>
      </div>
    </nav>
  )
}
