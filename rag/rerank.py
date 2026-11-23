"""Second-stage rerankers.

`HeuristicReranker` (default) blends query-term coverage, phrase overlap,
heading match and the first-stage signals into a calibrated 0..1 relevance
score, which is also what the "refuse when nothing relevant is found" rule
thresholds on. `CrossEncoderReranker` (`.[semantic]`) uses an ms-marco
MiniLM cross-encoder for neural reranking.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from rag.text import bigrams, tokenize


@dataclass
class Candidate:
    chunk_id: str
    text: str
    heading: str
    dense: float
    bm25: float
    rrf: float
    position: int = 0
    section: str = ""


class Reranker(Protocol):
    name: str

    def score(self, query: str, candidates: list[Candidate]) -> list[float]: ...


class HeuristicReranker:
    name = "heuristic-v2"

    def __init__(self) -> None:
        self.idf: dict[str, float] = {}  # set by the retriever from the BM25 index

    def _weight(self, term: str) -> float:
        return self.idf.get(term, max(self.idf.values(), default=1.0))

    def score(self, query: str, candidates: list[Candidate]) -> list[float]:
        # numbers in a question are parameters ("5 rooms"), not topics: they don't count towards coverage
        q_terms = {t for t in tokenize(query) if not t.isdigit()}
        q_bigrams = set(bigrams(tokenize(query)))
        if not q_terms:
            return [0.0 for _ in candidates]
        max_bm25 = max((c.bm25 for c in candidates), default=0.0) or 1.0
        scores = []
        for cand in candidates:
            body = set(tokenize(cand.text))
            head = set(tokenize(cand.heading))
            section = set(tokenize(cand.section)) if cand.section else head
            total = sum(self._weight(t) for t in q_terms) or 1.0
            # IDF-weighted: matching a rare term ('tax') says more than matching a common one ('rate')
            coverage = sum(self._weight(t) for t in q_terms & (body | head)) / total
            heading_match = sum(self._weight(t) for t in q_terms & section) / total  # the section's own title
            phrase = len(q_bigrams & set(bigrams(tokenize(cand.heading + " " + cand.text)))) / max(len(q_bigrams), 1)
            bm25_abs = 1 - math.exp(-cand.bm25 / 4)  # absolute keyword strength, saturating
            score = (
                0.40 * coverage
                + 0.16 * max(cand.dense, 0.0)
                + 0.11 * (cand.bm25 / max_bm25) * bm25_abs
                + 0.18 * heading_match
                + 0.10 * phrase
                + 0.05 * bm25_abs
                + 0.06 / (1 + cand.position)  # generic "what's the X policy" questions want the core rule first
            )
            scores.append(round(min(score, 1.0), 4))
        return scores


class CrossEncoderReranker:
    def __init__(self, model: str = "Xenova/ms-marco-MiniLM-L-6-v2") -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._model = TextCrossEncoder(model)
        self.name = f"cross-encoder-{model}"

    def score(self, query: str, candidates: list[Candidate]) -> list[float]:
        logits = list(self._model.rerank(query, [f"{c.heading}. {c.text}" for c in candidates]))
        return [round(1 / (1 + math.exp(-x)), 4) for x in logits]


def make_reranker(kind: str) -> Reranker:
    if kind == "cross-encoder":
        return CrossEncoderReranker()
    return HeuristicReranker()
