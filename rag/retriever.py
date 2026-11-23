"""Hybrid retriever: dense (Qdrant) + BM25, fused with Reciprocal Rank Fusion, then reranked.

Returns cited chunks, or an empty result when the best reranked score is below
the relevance threshold, so the Policy agent can refuse instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from common.config import Settings, get_settings
from common.metrics import RETRIEVAL_LATENCY
from common.telemetry import span
from rag.bm25 import BM25Index
from rag.documents import Chunk, SanitizeReport, chunk_documents, corpus_fingerprint, load_documents
from rag.embeddings import Embedder, make_embedder
from rag.rerank import Candidate, Reranker, make_reranker
from rag.store import QdrantStore

log = logging.getLogger(__name__)
RRF_K = 60


@dataclass
class RetrievedChunk:
    chunk: Chunk
    score: float
    dense: float
    bm25: float
    rrf: float

    def citation(self) -> dict:
        c = self.chunk
        return {
            "id": c.chunk_id,
            "title": c.title,
            "section": c.section,
            "page": c.page,
            "source": c.source,
            "score": self.score,
        }


@dataclass
class RetrievalResult:
    query: str
    chunks: list[RetrievedChunk] = field(default_factory=list)
    top_score: float = 0.0
    latency_ms: float = 0.0

    @property
    def found(self) -> bool:
        return bool(self.chunks)


class HybridRetriever:
    def __init__(
        self,
        chunks: list[Chunk],
        store: QdrantStore,
        embedder: Embedder,
        reranker: Reranker,
        min_score: float,
    ) -> None:
        self.chunks = chunks
        self.by_id = {c.chunk_id: c for c in chunks}
        self.store = store
        self.embedder = embedder
        self.reranker = reranker
        self.min_score = min_score
        self.bm25 = BM25Index([c.embed_text for c in chunks])
        if hasattr(reranker, "idf"):
            reranker.idf = self.bm25.idf

    def search_sync(self, query: str, k: int = 5, candidates: int = 20) -> RetrievalResult:
        started = time.perf_counter()
        dense_hits = self.store.search(self.embedder.embed([query])[0], candidates)
        dense_rank = {cid: (rank, score) for rank, (cid, score, _p) in enumerate(dense_hits)}
        bm25_scores = self.bm25.scores(query)
        bm25_top = sorted(
            ((self.chunks[i].chunk_id, s) for i, s in enumerate(bm25_scores) if s > 0), key=lambda x: x[1], reverse=True
        )[:candidates]
        bm25_rank = {cid: (rank, score) for rank, (cid, score) in enumerate(bm25_top)}
        bm25_by_id = {self.chunks[i].chunk_id: s for i, s in enumerate(bm25_scores)}

        fused: dict[str, float] = {}
        for ranking in (dense_rank, bm25_rank):
            for cid, (rank, _score) in ranking.items():
                fused[cid] = fused.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
        pool = sorted(fused, key=fused.get, reverse=True)[:candidates]
        cands = [
            Candidate(
                chunk_id=cid,
                text=self.by_id[cid].text,
                heading=f"{self.by_id[cid].title} {self.by_id[cid].section}",
                dense=dense_rank.get(cid, (0, 0.0))[1],
                bm25=bm25_by_id.get(cid, 0.0),
                rrf=fused[cid],
                position=self.by_id[cid].position,
                section=self.by_id[cid].section,
            )
            for cid in pool
            if cid in self.by_id
        ]
        scores = self.reranker.score(query, cands) if cands else []
        ranked = sorted(zip(cands, scores, strict=True), key=lambda x: x[1], reverse=True)
        top = ranked[0][1] if ranked else 0.0
        kept = [
            RetrievedChunk(self.by_id[c.chunk_id], s, c.dense, c.bm25, c.rrf)
            for c, s in ranked[:k]
            if top >= self.min_score and s >= self.min_score * 0.6
        ]
        latency = (time.perf_counter() - started) * 1000
        RETRIEVAL_LATENCY.observe(latency / 1000)
        return RetrievalResult(query, kept, top, latency)

    async def search(self, query: str, k: int = 5) -> RetrievalResult:
        with span("rag.search", **{"rag.query_chars": len(query)}) as current:
            result = await asyncio.to_thread(self.search_sync, query, k)
            current.set_attribute("rag.top_score", result.top_score)
            current.set_attribute("rag.hits", len(result.chunks))
            return result


def build_retriever(settings: Settings | None = None, store: QdrantStore | None = None) -> tuple[HybridRetriever, dict]:
    settings = settings or get_settings()
    report = SanitizeReport()
    chunks = chunk_documents(load_documents(settings.policies_dir), report=report)
    embedder = make_embedder(settings.embedding_provider)
    if store is None:
        if settings.qdrant_url:
            store = QdrantStore(settings.qdrant_collection, url=settings.qdrant_url)
        else:
            store = QdrantStore(settings.qdrant_collection)  # embedded, in-memory
    version = corpus_fingerprint(chunks, embedder.name)
    rebuilt = False
    if store.fingerprint() != version:
        vectors = embedder.embed([c.embed_text for c in chunks])
        store.rebuild(chunks, vectors, version)
        rebuilt = True
    retriever = HybridRetriever(chunks, store, embedder, make_reranker(settings.reranker), settings.retrieval_min_score)
    stats = {
        "chunks": len(chunks),
        "documents": len({c.doc_id for c in chunks}),
        "index_version": version,
        "rebuilt": rebuilt,
        "embedder": embedder.name,
        "reranker": retriever.reranker.name,
        "quarantined": report.quarantined,
        "hidden_markup_removed": report.removed_hidden_markup,
    }
    log.info("RAG index ready: %s", {k: v for k, v in stats.items() if k != "quarantined"})
    return retriever, stats


_retriever: HybridRetriever | None = None
_stats: dict = {}


def get_retriever() -> HybridRetriever:
    global _retriever, _stats
    if _retriever is None:
        _retriever, _stats = build_retriever()
    return _retriever


def retriever_stats() -> dict:
    return _stats


def set_retriever(retriever: HybridRetriever | None) -> None:
    global _retriever
    _retriever = retriever
