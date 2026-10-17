import type { ComponentType, SVGProps } from 'react'

import { ChatIcon, DashboardIcon, DocumentsIcon, EvaluationIcon, SearchIcon } from './icons'

export interface NavItem {
  to: string
  label: string
  icon: ComponentType<SVGProps<SVGSVGElement>>
  available: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: DashboardIcon, available: true },
  { to: '/documents', label: 'Documents', icon: DocumentsIcon, available: true },
  { to: '/search', label: 'Search', icon: SearchIcon, available: true },
  { to: '/chat', label: 'Chat', icon: ChatIcon, available: true },
  { to: '/evaluation', label: 'Evaluation', icon: EvaluationIcon, available: false },
]

export function pageTitle(pathname: string): string {
  const match = NAV_ITEMS.find((item) => item.to === pathname)
  if (match) return match.label
  if (/^\/documents\/[^/]+\/?$/.test(pathname)) return 'Document details'
  if (/^\/chat\/[^/]+\/?$/.test(pathname)) return 'Chat'
  return 'Not found'
}
