"""Embedders.

* `HashingEmbedder` (default): deterministic signed feature hashing of stems,
  bigrams and character trigrams. Zero downloads, so CI and offline demos work
  anywhere, and evals are reproducible.
* `FastEmbedEmbedder` (`pip install .[semantic]`): BAAI/bge-small-en-v1.5 ONNX
  model for real semantic similarity.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from typing import Protocol

import numpy as np

from rag.text import bigrams, tokenize


class Embedder(Protocol):
    name: str
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


def _bucket(feature: str, dim: int) -> tuple[int, float]:
    digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
    value = int.from_bytes(digest, "little")
    return value % dim, 1.0 if (value >> 63) & 1 else -1.0


class HashingEmbedder:
    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _features(self, text: str) -> Counter[str]:
        tokens = tokenize(text)
        feats: Counter[str] = Counter()
        for tok in tokens:
            feats[f"w:{tok}"] += 1.0
            padded = f"#{tok}#"
            for i in range(len(padded) - 2):
                feats[f"c:{padded[i:i + 3]}"] += 0.25
        for bg in bigrams(tokens):
            feats[f"b:{bg}"] += 0.6
        return feats

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            for feature, weight in self._features(text).items():
                idx, sign = _bucket(feature, self.dim)
                out[row, idx] += sign * (1 + math.log(weight)) if weight >= 1 else sign * weight
            norm = np.linalg.norm(out[row])
            if norm > 0:
                out[row] /= norm
        return out


class FastEmbedEmbedder:
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model)
        self.name = f"fastembed-{model}"
        self.dim = len(next(iter(self._model.embed(["probe"]))))

    def embed(self, texts: list[str]) -> np.ndarray:
        vectors = np.array(list(self._model.embed(texts)), dtype=np.float32)
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        return vectors / np.clip(norms, 1e-9, None)


def make_embedder(provider: str) -> Embedder:
    if provider == "fastembed":
        return FastEmbedEmbedder()
    return HashingEmbedder()
