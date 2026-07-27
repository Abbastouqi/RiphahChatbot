"""In-process vector store: the whole embedding matrix held in RAM.

Deliberately not a vector database. ~3k chunks x 1536 dims x 4 bytes is under
20 MB, and a single numpy matmul over that is ~2 ms — well inside a voice
latency budget, with no extra service to run or keep in sync.

`search()` is the only surface the rest of the app uses. To move to pgvector or
Qdrant later, reimplement that one function and leave callers untouched.
"""
from __future__ import annotations

import threading

import numpy as np

import config
from kb import db, embed


class VectorStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._matrix: np.ndarray | None = None
        self._ids: list[int] = []
        self._meta: dict[int, dict] = {}
        self._loaded_count = 0

    def load(self, *, force: bool = False) -> None:
        with self._lock:
            if self._matrix is not None and not force:
                return
            conn = db.connect()
            try:
                rows = conn.execute(
                    """
                    SELECT c.id, c.url, c.heading, c.text, c.section, c.faculty,
                           c.campus, c.embedding, p.title, p.fetched_at
                      FROM chunks c
                      JOIN pages p ON p.url = c.url
                     WHERE c.embedding IS NOT NULL
                     ORDER BY c.id
                    """
                ).fetchall()
            finally:
                conn.close()

            if not rows:
                self._matrix = np.zeros((0, config.EMBED_DIMENSIONS), dtype=np.float32)
                self._ids, self._meta, self._loaded_count = [], {}, 0
                return

            vectors = np.vstack([embed.from_blob(r["embedding"]) for r in rows])
            self._matrix = vectors
            self._ids = [r["id"] for r in rows]
            self._meta = {
                r["id"]: {
                    "chunk_id": r["id"],
                    "url": r["url"],
                    "title": r["title"],
                    "heading": r["heading"],
                    "text": r["text"],
                    "section": r["section"],
                    "faculty": r["faculty"],
                    "campus": r["campus"],
                    "fetched_at": r["fetched_at"],
                }
                for r in rows
            }
            self._loaded_count = len(rows)

    @property
    def size(self) -> int:
        return self._loaded_count

    def search(self, query_vector: list[float] | np.ndarray, *,
               top_k: int = config.DEFAULT_TOP_K,
               section: str | None = None,
               faculty: str | None = None) -> list[dict]:
        """Cosine similarity over the whole matrix. Vectors are pre-normalised at
        write time, so this is a dot product."""
        self.load()
        if self._matrix is None or self._matrix.shape[0] == 0:
            return []

        query = np.asarray(query_vector, dtype=np.float32)
        norm = np.linalg.norm(query)
        if norm > 0:
            query = query / norm

        scores = self._matrix @ query

        # Metadata filters are applied by masking scores rather than slicing the
        # matrix, so the hot path stays a single contiguous matmul.
        if section or faculty:
            mask = np.ones(len(self._ids), dtype=bool)
            for index, chunk_id in enumerate(self._ids):
                meta = self._meta[chunk_id]
                if section and meta.get("section") != section:
                    mask[index] = False
                elif faculty and meta.get("faculty") != faculty:
                    mask[index] = False
            scores = np.where(mask, scores, -1.0)

        count = min(top_k, len(scores))
        top = np.argpartition(-scores, count - 1)[:count]
        top = top[np.argsort(-scores[top])]

        results = []
        for index in top:
            score = float(scores[index])
            if score < config.MIN_SIMILARITY:
                continue
            item = dict(self._meta[self._ids[index]])
            item["similarity"] = round(score, 4)
            results.append(item)
        return results


STORE = VectorStore()
