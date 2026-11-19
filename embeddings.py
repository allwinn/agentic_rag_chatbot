"""
Shared embedding helper, used by build_index.py (documents) and the agent (queries).
Both sides MUST use the same model, so it lives in one place.

Default: a small multilingual model (Arabic + English), run locally via fastembed (ONNX, CPU).
  paraphrase-multilingual-MiniLM-L12-v2 -> 384 dims, ~220 MB, no API key, no cost.

EMBED_BACKEND=hash gives a crude offline stand-in for testing the pipeline without
downloading the model. Never use it for the real index.
"""
from __future__ import annotations

import hashlib
import os
import re

import numpy as np

MODEL_NAME = os.getenv("EMBED_MODEL", "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
BACKEND = os.getenv("EMBED_BACKEND", "fastembed")
DIM = 384

_model = None


def _hash_embed(text: str) -> np.ndarray:
    v = np.zeros(DIM, dtype=np.float32)
    for tok in re.findall(r"\w+", text.lower()):
        h = int(hashlib.md5(tok.encode()).hexdigest(), 16)
        v[h % DIM] += 1.0
    n = np.linalg.norm(v)
    return v / n if n else v


def embed(texts: list[str], batch_size: int = 64) -> np.ndarray:
    """Return an (n, DIM) array of L2-normalised vectors."""
    global _model
    if BACKEND == "hash":
        return np.stack([_hash_embed(t) for t in texts]) if texts else np.zeros((0, DIM), np.float32)
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
    vecs = np.array(list(_model.embed(texts, batch_size=batch_size)), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return vecs


def embed_query(text: str) -> np.ndarray:
    return embed([text])[0]
