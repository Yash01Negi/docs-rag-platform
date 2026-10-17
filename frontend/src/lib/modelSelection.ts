// The chat model selection: which catalog entry to use, remembered for the browser session.

import type { ModelCatalog, ModelChoice, ModelCost, ModelEntry } from './chat'

/** sessionStorage key: the choice lasts until the tab or browser is closed. */
export const MODEL_STORAGE_KEY = 'yashrag.chat.model'

export const COST_LABELS: Record<ModelCost, string> = {
  paid: 'Paid',
  free_tier: 'Free tier',
  local: 'Local',
}

/** A stable key for a choice ("|" never occurs in provider names or model IDs). */
export function modelKey(choice: ModelChoice): string {
  return `${choice.provider}|${choice.model}`
}

export function loadStoredChoice(): ModelChoice | null {
  try {
    const raw = window.sessionStorage.getItem(MODEL_STORAGE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (
      parsed &&
      typeof parsed === 'object' &&
      typeof (parsed as ModelChoice).provider === 'string' &&
      typeof (parsed as ModelChoice).model === 'string'
    ) {
      return { provider: (parsed as ModelChoice).provider, model: (parsed as ModelChoice).model }
    }
  } catch {
    // Storage blocked or corrupted: fall back to the server's default.
  }
  return null
}

export function storeChoice(choice: ModelChoice): void {
  try {
    window.sessionStorage.setItem(
      MODEL_STORAGE_KEY,
      JSON.stringify({ provider: choice.provider, model: choice.model }),
    )
  } catch {
    // Not remembered; the choice still applies to this page.
  }
}

/**
 * The model to preselect: the remembered one while the server still lists it (even when it
 * is unavailable right now: the page then says so and does not send, rather than switching
 * to another, possibly paid, model on its own), else the server's default, else the first
 * usable model, else null.
 */
export function chooseModel(catalog: ModelCatalog, stored: ModelChoice | null): ModelEntry | null {
  const find = (choice: ModelChoice | null) =>
    choice ? catalog.models.find((m) => modelKey(m) === modelKey(choice)) : undefined
  const remembered = find(stored)
  if (remembered) return remembered
  const fallback = find(catalog.default)
  if (fallback?.selectable) return fallback
  return catalog.models.find((m) => m.selectable) ?? fallback ?? null
}
