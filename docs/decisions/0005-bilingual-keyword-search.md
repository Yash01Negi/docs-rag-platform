# ADR 0005: Bilingual keyword search with Sudachi tokens in a PostgreSQL tsvector

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Phase 3 adds keyword search next to semantic search. Keyword search has to work for
English, for Japanese, and for text that mixes both. Most queries look like these:

- English words and phrases (`point-in-time recovery`, `"incident commander"`), where
  `backups` should also match `backup`;
- Japanese written without spaces (`在宅勤務の申請期限`), which needs morphological analysis
  to find word boundaries;
- mixed queries (`helm rollback の手順`, `access token の有効期限`);
- full-width and half-width forms and variant spellings (`ＶＰＮ` and `VPN`, `サーバ` and
  `サーバー`).

PostgreSQL's built-in full-text search (`to_tsvector('english', ...)`) splits on spaces and
punctuation. A Japanese sentence becomes one long token or is broken at script changes, so
Japanese words cannot be matched. PostgreSQL ships no Japanese text search configuration.
Using it for Japanese would silently return wrong results, so it was ruled out.

Options considered:

1. **PGroonga.** A PostgreSQL extension that indexes text with the Groonga engine and
   supports Japanese with N-gram or MeCab tokenizers. It is mature and fast, and queries
   stay in SQL. However, it is not available on common managed PostgreSQL services (for
   example Amazon RDS or Cloud SQL), and running it next to pgvector needs a custom
   PostgreSQL image that the project would have to build and update. Its default bigram
   tokenizer also matches substrings across word boundaries (`京都` inside `東京都`), and
   ranking is not BM25.
2. **An external search engine** (Elasticsearch or OpenSearch with the kuromoji or Sudachi
   plugin). Strong Japanese support, but a second datastore to run, secure and keep in sync
   with PostgreSQL, which is out of proportion for this project.
3. **Tokenize in the application and store the tokens in a PostgreSQL `tsvector`.** Python
   does the language-specific analysis; PostgreSQL stores the terms and positions, indexes
   them with GIN, and evaluates matches and phrases with `tsquery`. Works on any
   PostgreSQL with no extension beyond pgvector.

## Decision

Use **option 3**: application-side analysis with **SudachiPy** (dictionary
`sudachidict-core`) for Japanese and **Snowball** stemming for English, stored in
`chunks.search_vector` (`tsvector`, GIN-indexed) with the term count in
`chunks.search_length`. Ranking is **Okapi BM25** computed in SQL.

### Analyzer (`app/search/analyzer.py`)

- Sudachi tokenizes the NFC text in **split mode C** (longest units, so `在宅勤務` is one
  term). Compound words are also indexed as their **mode A** parts at the same positions,
  so `勤務` alone still matches `在宅勤務`.
- Japanese terms use Sudachi's **normalized form**, which unifies spelling variants and
  character widths (`サーバ` → `サーバー`, `附属` → `付属`, half-width katakana → full-width).
- Particles, auxiliary verbs, symbols and whitespace (`助詞`, `助動詞`, `補助記号`, `記号`,
  `空白`) are not indexed, and a short list of very common light verbs and nouns is skipped
  (`為る`, `有る`, `居る`, `成る`, `出来る`, `事`, `物`, `為`). Skipped tokens still advance
  the position counter (except whitespace), so phrase distances stay correct.
- Latin-script tokens are **not** taken from Sudachi's normalized form, because Sudachi maps
  some English words to katakana. Instead they are NFKC-normalized, case-folded, split on
  `[0-9a-z]+(?:'[a-z]+)*`, filtered with the Lucene English stopword list, and alphabetic
  words are stemmed with the Snowball English stemmer (`backups` → `backup`).
- Queries go through the same analyzer. Text in `"..."`, `“...”` or `「...」` becomes a
  phrase: a `tsquery` chain of `<N>` distance operators that uses the same positions.
- `ANALYZER_VERSION` names the analyzer configuration. Changing the analyzer needs
  `python -m app.worker rebuild-search-index`, which recomputes every chunk's vector.

### Ranking

A chunk is a candidate when it contains **any** query term and **all** quoted phrases. Its
score is BM25 with k1 = 1.2 and b = 0.75 (configurable), using term frequencies from the
stored positions, document frequency per term over all chunks, and the average chunk length.
Ties are broken by chunk ID so results are deterministic.

### Dependencies and licences

- SudachiPy 0.7 (Apache-2.0), with a compiled Rust core; wheels for Linux, macOS and
  Windows on CPython 3.12.
- sudachidict-core (Apache-2.0); the installed dictionary is about 190 MB. It loads in well
  under a second.
- snowballstemmer 3.1 (BSD-3-Clause), pure Python.

## Consequences

**Benefits**

- Japanese, English and mixed text are searched with real word boundaries, normalization
  and stemming, and quoted phrases work in both languages.
- No PostgreSQL extension beyond pgvector, so the database runs on stock images and on
  managed services.
- Keyword and vector data live in the same row and the same transaction, so a document is
  never searchable by one method and not the other.
- Scores are standard BM25, which is easy to explain and to tune.

**Costs and risks**

- **BM25 is computed at query time.** The GIN index finds candidate chunks, but document
  frequencies and term frequencies are computed per query over the matching rows. This is
  fast for the corpus sizes this project targets (measured in the evaluation report), but a
  query term that appears in a large share of millions of chunks would be slow. A
  precomputed term-statistics table is the next step if that happens.
- **Two analyzers must agree.** Indexing and querying must use the same analyzer version.
  The version is recorded in code, and changing it needs a rebuild of the search vectors.
- **Dictionary-based analysis has gaps.** New product names and rare words that Sudachi
  does not know are split into shorter units. They are still matched, as a sequence of
  parts, but the match can be looser than intended.
- **Image size.** The Sudachi dictionary adds about 190 MB to the backend images.
- **No cross-language keyword matching.** `password` does not match `パスワード`. That is the
  job of semantic search, and hybrid mode combines both.
