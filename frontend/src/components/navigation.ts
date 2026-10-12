import type { ComponentType, SVGProps } from 'react'

import { ChatIcon, DashboardIcon, DocumentsIcon, EvaluationIcon } from './icons'

export interface NavItem {
  to: string
  label: string
  icon: ComponentType<SVGProps<SVGSVGElement>>
  available: boolean
}

export const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: DashboardIcon, available: true },
  { to: '/documents', label: 'Documents', icon: DocumentsIcon, available: false },
  { to: '/chat', label: 'Chat', icon: ChatIcon, available: false },
  { to: '/evaluation', label: 'Evaluation', icon: EvaluationIcon, available: false },
]

export function pageTitle(pathname: string): string {
  const match = NAV_ITEMS.find((item) => item.to === pathname)
  return match?.label ?? 'Not found'
}
