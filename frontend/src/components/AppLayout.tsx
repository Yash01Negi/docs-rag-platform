import { useState } from 'react'
import { Outlet, useLocation } from 'react-router'

import { useBackendStatus, type BackendStatus } from '../hooks/useBackendStatus'
import { MenuIcon } from './icons'
import { pageTitle } from './navigation'
import Sidebar from './Sidebar'
import StatusBadge from './StatusBadge'

/** Data shared with every page through the router outlet. */
export interface LayoutContext {
  backend: BackendStatus
}

export default function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const backend = useBackendStatus()
  const { pathname } = useLocation()

  return (
    <div className="min-h-screen">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50 focus:rounded-md focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow"
      >
        Skip to content
      </a>

      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />

      <div className="lg:pl-64">
        <header className="sticky top-0 z-20 flex h-16 items-center gap-3 border-b border-slate-200 bg-white/90 px-4 backdrop-blur sm:px-6 lg:px-8">
          <button
            type="button"
            onClick={() => setSidebarOpen(true)}
            className="-ml-1 rounded-md p-2 text-slate-600 hover:bg-slate-100 focus-visible:outline-2 focus-visible:outline-indigo-600 lg:hidden"
            aria-label="Open navigation"
            aria-controls="app-sidebar"
            aria-expanded={sidebarOpen}
          >
            <MenuIcon className="h-5 w-5" />
          </button>
          <h1 className="flex-1 truncate text-base font-semibold text-slate-900">
            {pageTitle(pathname)}
          </h1>
          <StatusBadge state={backend.state} />
        </header>

        <main id="main-content" className="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
          <Outlet context={{ backend } satisfies LayoutContext} />
        </main>
      </div>
    </div>
  )
}
