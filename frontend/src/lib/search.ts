import type { ScoreType } from './api'

export const SCORE_LABELS: Record<ScoreType, string> = {
  cosine_similarity: 'Cosine similarity',
  bm25: 'BM25',
  rrf: 'RRF',
}
