# ADR 0004: Hybrid retrieval with a calibrated refusal threshold and native citations

- Status: accepted
- Date: 2026-11-24

## Context

Policy answers must be correct and traceable, and the assistant must say "I don't know" instead of guessing.
Policy text is full of exact terms (48 hours, WINTER10, no-show) that pure vector search handles poorly.

## Decision

- Index policy PDFs (page-aware), Markdown policies and FAQs, chunked by section; vectors in **Qdrant** (embedded mode
  for dev/tests, server in compose/k8s).
- Query with **dense + BM25**, fuse with **Reciprocal Rank Fusion** (k=60), then **rerank** (IDF-weighted term
  coverage, section-title match, phrase overlap, first-stage scores; optional ms-marco cross-encoder).
- **Refuse** when the best reranked score is below 0.30 (calibrated on the labelled set: in-domain questions score
  0.44 to 0.92, off-topic ones 0.01 to 0.21).
- LLM answers use Claude's **native citations** (each chunk is a `document` block with citations enabled); an answer
  with no citation is discarded and replaced by an extractive, always-cited answer.
- **Document sanitising** at ingestion: strip hidden markup and invisible characters, and quarantine sentences that
  address an AI or contain instructions (see the write-up in `docs/writeups/indirect-prompt-injection.md`).

## Consequences

- Offline results: Recall@5 1.00, MRR 0.98, refusal accuracy 1.00 on 54 labelled + 10 unanswerable questions.
- The default `hashing` embedder needs no downloads, so CI is deterministic; `EMBEDDING_PROVIDER=fastembed` switches
  to `bge-small-en-v1.5` for real semantic matching. Re-run `make eval` after switching: the refusal threshold is
  calibrated for the default.
