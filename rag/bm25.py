"""BM25 (Okapi) keyword index: the lexical half of hybrid search."""

from __future__ import annotations

import math
from collections import Counter

from rag.text import tokenize


class BM25Index:
    def __init__(self, docs: list[str], k1: float = 1.4, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.tokens = [tokenize(d) for d in docs]
        self.tfs = [Counter(t) for t in self.tokens]
        self.lengths = [len(t) for t in self.tokens]
        self.avgdl = sum(self.lengths) / max(len(self.lengths), 1)
        df: Counter[str] = Counter()
        for tf in self.tfs:
            df.update(tf.keys())
        n = len(docs)
        self.idf = {term: math.log(1 + (n - freq + 0.5) / (freq + 0.5)) for term, freq in df.items()}

    def scores(self, query: str) -> list[float]:
        q_terms = tokenize(query)
        out = []
        for tf, length in zip(self.tfs, self.lengths, strict=True):
            score = 0.0
            for term in q_terms:
                if term not in tf:
                    continue
                freq = tf[term]
                denom = freq + self.k1 * (1 - self.b + self.b * length / (self.avgdl or 1))
                score += self.idf.get(term, 0.0) * freq * (self.k1 + 1) / denom
            out.append(score)
        return out

    def top(self, query: str, k: int) -> list[tuple[int, float]]:
        scored = [(i, s) for i, s in enumerate(self.scores(query)) if s > 0]
        return sorted(scored, key=lambda x: x[1], reverse=True)[:k]
