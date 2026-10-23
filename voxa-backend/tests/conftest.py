"""
Test-suite bootstrap.

The unit tests must be hermetic: no model downloads, no GPU, no Redis/PostgreSQL.
Heavy ML dependencies are therefore replaced with small deterministic test doubles
*before* any application module is imported. Production code imports the real
packages and fails loudly if they are missing.
"""
import hashlib
import re
import sys
import types

import numpy as np


class FakeSentenceTransformer:
    """
    Deterministic bag-of-words hashing encoder (384-d, L2-normalised).
    Identical texts map to identical vectors (cosine 1.0) and texts sharing words are
    closer than unrelated texts, which is all the cache/RAG logic needs.
    """
    DIM = 384

    def __init__(self, model_name: str = "", *args, **kwargs):
        self.model_name = model_name

    def encode(self, text, normalize_embeddings: bool = True):
        vec = np.zeros(self.DIM, dtype=np.float32)
        for word in re.findall(r"[a-z0-9]+", str(text).lower()):
            vec[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.DIM] += 1.0
        norm = np.linalg.norm(vec)
        if normalize_embeddings and norm > 0:
            vec /= norm
        return vec


# Always stub the embedding model: the real one would download weights from HuggingFace.
_st = types.ModuleType("sentence_transformers")
_st.SentenceTransformer = FakeSentenceTransformer
sys.modules["sentence_transformers"] = _st

# Worker-only dependencies are stubbed only when not installed (gateway-only dev envs).
try:  # pragma: no cover - depends on the environment
    import faster_whisper  # noqa: F401
except ImportError:  # pragma: no cover
    _fw = types.ModuleType("faster_whisper")

    class WhisperModel:  # minimal stand-in; tests construct workers with load_model=False
        def __init__(self, *args, **kwargs):
            raise RuntimeError("faster-whisper is not installed")

    _fw.WhisperModel = WhisperModel
    sys.modules["faster_whisper"] = _fw

try:  # pragma: no cover
    import edge_tts  # noqa: F401
except ImportError:  # pragma: no cover
    sys.modules["edge_tts"] = types.ModuleType("edge_tts")
