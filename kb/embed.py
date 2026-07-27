"""Stage 5: embed chunks with OpenAI and store the vectors in SQLite.

Vectors are float32 BLOBs on the `chunks` row. At Riphah's scale (a few thousand
chunks) a numpy dot product over the whole matrix takes single-digit milliseconds
— far below the latency budget — so there's no vector database to operate. See
vector_store.py for the swap-in point if the corpus ever outgrows that.
"""
from __future__ import annotations

import os

import numpy as np

import config
from kb import db

BATCH_SIZE = 96          # OpenAI accepts more, but this keeps request bodies small
MAX_CHARS = 8000         # ~2k tokens; longer chunks get truncated rather than rejected


def _client():
    from openai import OpenAI

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set — add it to .env")
    return OpenAI()


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch. `dimensions` shortens the vector natively (Matryoshka),
    which halves storage versus the model's default 3072 with no re-normalising."""
    client = _client()
    payload = [t[:MAX_CHARS] if t.strip() else " " for t in texts]
    response = client.embeddings.create(
        model=config.EMBED_MODEL,
        input=payload,
        dimensions=config.EMBED_DIMENSIONS,
    )
    return [item.embedding for item in response.data]


def to_blob(vector: list[float] | np.ndarray) -> bytes:
    array = np.asarray(vector, dtype=np.float32)
    # Normalise once at write time so retrieval is a plain dot product.
    norm = np.linalg.norm(array)
    if norm > 0:
        array = array / norm
    return array.tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def run(*, rebuild: bool = False, batch_size: int = BATCH_SIZE) -> int:
    conn = db.connect()
    embedded = 0
    try:
        if rebuild:
            conn.execute("UPDATE chunks SET embedding = NULL, embed_model = NULL")
            conn.commit()

        # Re-embed anything missing a vector, or embedded by a different model.
        pending = conn.execute(
            "SELECT id, text FROM chunks "
            "WHERE embedding IS NULL OR embed_model IS NOT ? "
            "ORDER BY id",
            (config.EMBED_MODEL,),
        ).fetchall()

        if not pending:
            print("  embed: nothing to do", flush=True)
            return 0

        print(f"  embed: {len(pending)} chunks pending", flush=True)
        for start in range(0, len(pending), batch_size):
            batch = pending[start:start + batch_size]
            vectors = embed_texts([row["text"] for row in batch])
            for row, vector in zip(batch, vectors):
                conn.execute(
                    "UPDATE chunks SET embedding = ?, embed_model = ? WHERE id = ?",
                    (to_blob(vector), config.EMBED_MODEL, row["id"]),
                )
            embedded += len(batch)
            conn.commit()
            print(f"  embed: {embedded}/{len(pending)}", flush=True)
    finally:
        conn.close()

    return embedded


if __name__ == "__main__":
    db.migrate()
    with db.stage("embed") as result:
        result["items"] = run()
