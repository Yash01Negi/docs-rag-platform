import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ModelCatalog, ModelEntry } from './chat'
import { MODEL_STORAGE_KEY, chooseModel, loadStoredChoice, modelKey, storeChoice } from './modelSelection'

function entry(provider: string, model: string, overrides: Partial<ModelEntry> = {}): ModelEntry {
  return {
    provider,
    model,
    provider_label: provider,
    cost: 'paid',
    configured: true,
    availability: { state: 'available', reason: null },
    selectable: true,
    default: false,
    ...overrides,
  }
}

const catalog = (models: ModelEntry[], def = { provider: 'anthropic', model: 'claude' }): ModelCatalog => ({
  providers: [],
  models,
  default: def,
})

beforeEach(() => window.sessionStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('chooseModel', () => {
  const claude = entry('anthropic', 'claude', { default: true })
  const gemini = entry('gemini', 'gemini-flash', { cost: 'free_tier' })
  const qwen = entry('ollama', 'qwen3:8b', { cost: 'local' })

  it('prefers the remembered model, then the default, then the first usable one', () => {
    expect(chooseModel(catalog([claude, gemini]), { provider: 'gemini', model: 'gemini-flash' })).toBe(gemini)
    expect(chooseModel(catalog([claude, gemini]), null)).toBe(claude)
    expect(chooseModel(catalog([claude, gemini]), { provider: 'gemini', model: 'gone' })).toBe(claude)
    const noDefault = catalog([{ ...claude, selectable: false }, qwen])
    expect(chooseModel(noDefault, null)).toBe(qwen)
  })

  it('keeps a remembered model that is now unavailable instead of switching', () => {
    const down = { ...gemini, selectable: false }
    expect(chooseModel(catalog([claude, down]), { provider: 'gemini', model: 'gemini-flash' })).toBe(down)
  })

  it('returns the default (or nothing) when no model can be used', () => {
    const none = catalog([{ ...claude, selectable: false }])
    expect(chooseModel(none, null)?.model).toBe('claude')
    expect(chooseModel(catalog([], { provider: 'x', model: 'y' }), null)).toBeNull()
  })
})

describe('session storage', () => {
  it('stores and loads a choice', () => {
    storeChoice({ provider: 'ollama', model: 'qwen3:8b' })
    expect(JSON.parse(window.sessionStorage.getItem(MODEL_STORAGE_KEY) ?? '')).toEqual({
      provider: 'ollama',
      model: 'qwen3:8b',
    })
    expect(loadStoredChoice()).toEqual({ provider: 'ollama', model: 'qwen3:8b' })
  })

  it('ignores corrupt values and blocked storage', () => {
    window.sessionStorage.setItem(MODEL_STORAGE_KEY, '{"provider": 1}')
    expect(loadStoredChoice()).toBeNull()
    window.sessionStorage.setItem(MODEL_STORAGE_KEY, 'not json')
    expect(loadStoredChoice()).toBeNull()
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(loadStoredChoice()).toBeNull()
    expect(() => storeChoice({ provider: 'a', model: 'b' })).not.toThrow()
  })

  it('builds keys that keep Ollama tags intact', () => {
    expect(modelKey({ provider: 'ollama', model: 'qwen3:8b' })).toBe('ollama|qwen3:8b')
  })
})
