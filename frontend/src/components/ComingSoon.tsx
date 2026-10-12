import type { ComponentType, SVGProps } from 'react'

import { ClockIcon } from './icons'

interface ComingSoonProps {
  icon: ComponentType<SVGProps<SVGSVGElement>>
  title: string
  description: string
  planned: string[]
}

/** Placeholder for a module that is planned but not implemented yet. */
export default function ComingSoon({ icon: ModuleIcon, title, description, planned }: ComingSoonProps) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-6 shadow-sm sm:p-10">
      <div className="mx-auto max-w-xl text-center">
        <div className="mx-auto flex h-12 w-12 items-center justify-center rounded-xl bg-indigo-50 text-indigo-600">
          <ModuleIcon className="h-6 w-6" />
        </div>
        <h2 className="mt-4 text-xl font-semibold text-slate-900">{title}</h2>
        <p className="mt-2 text-sm leading-6 text-slate-600">{description}</p>
        <p className="mt-4 inline-flex items-center gap-1.5 rounded-full bg-amber-50 px-3 py-1 text-xs font-medium text-amber-800 ring-1 ring-amber-200 ring-inset">
          <ClockIcon className="h-3.5 w-3.5" />
          Coming in a later milestone
        </p>
      </div>

      <div className="mx-auto mt-8 max-w-xl rounded-lg border border-dashed border-slate-300 bg-slate-50 p-5">
        <h3 className="text-xs font-semibold tracking-wide text-slate-500 uppercase">
          Planned scope (not implemented)
        </h3>
        <ul className="mt-3 space-y-2 text-sm text-slate-700">
          {planned.map((item) => (
            <li key={item} className="flex gap-2">
              <span className="mt-2 h-1.5 w-1.5 shrink-0 rounded-full bg-slate-400" aria-hidden="true" />
              {item}
            </li>
          ))}
        </ul>
      </div>
    </section>
  )
}
