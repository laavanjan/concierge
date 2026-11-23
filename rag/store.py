"""Qdrant vector store. Server mode in compose/k8s; embedded (local) mode for dev and tests."""

from __future__ import annotations

import uuid
from typing import Any

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models as qm

from rag.documents import Chunk

_NAMESPACE = uuid.UUID("7d5b1c1e-4f3a-4c55-9b7e-2f3c1d0a9e10")


class QdrantStore:
    def __init__(self, collection: str, url: str | None = None, path: str | None = None) -> None:
        self.collection = collection
        if url:
            self.client = QdrantClient(url=url, timeout=10)
        elif path:
            self.client = QdrantClient(path=path)
        else:
            self.client = QdrantClient(":memory:")

    def fingerprint(self) -> str | None:
        if not self.client.collection_exists(self.collection):
            return None
        points, _ = self.client.scroll(self.collection, limit=1, with_payload=True, with_vectors=False)
        return points[0].payload.get("index_version") if points else None

    def rebuild(self, chunks: list[Chunk], vectors: np.ndarray, version: str) -> None:
        if self.client.collection_exists(self.collection):
            self.client.delete_collection(self.collection)
        self.client.create_collection(
            self.collection,
            vectors_config=qm.VectorParams(size=int(vectors.shape[1]), distance=qm.Distance.COSINE),
        )
        points = [
            qm.PointStruct(
                id=str(uuid.uuid5(_NAMESPACE, chunk.chunk_id)),
                vector=vector.tolist(),
                payload={**chunk.payload(), "index_version": version},
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        for start in range(0, len(points), 128):
            self.client.upsert(self.collection, points=points[start : start + 128], wait=True)

    def search(self, vector: np.ndarray, limit: int) -> list[tuple[str, float, dict[str, Any]]]:
        result = self.client.query_points(
            self.collection, query=vector.tolist(), limit=limit, with_payload=True, with_vectors=False
        )
        return [(p.payload["chunk_id"], float(p.score), p.payload) for p in result.points]

    def close(self) -> None:
        self.client.close()
