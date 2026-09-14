"""Deterministic local retrieval primitives for hybrid chunk ranking.

The bundled encoder is a hashed word/character n-gram lexical proxy. It is not
a neural embedding model and performs no network or cloud calls. Providers can
inject another encoder implementing :class:`SemanticEncoder`.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from typing import Protocol, TypeVar


class SemanticEncoder(Protocol):
    """Interface for local or remote semantic encoders with vector output."""

    def encode(self, text: str) -> Sequence[float]: ...


class LocalNgramEncoder:
    """Encode text with deterministic hashed word and character n-grams."""

    def __init__(self, dimensions: int = 2048) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions

    def encode(self, text: str) -> tuple[float, ...]:
        words = re.findall(r"\w+", text.casefold())
        features: Counter[str] = Counter()
        for word in words:
            features[f"w:{word}"] += 2.0
            padded = f"^{word}$"
            for size in range(2, min(4, len(padded)) + 1):
                for index in range(len(padded) - size + 1):
                    features[f"c{size}:{padded[index:index + size]}"] += 0.5

        vector = [0.0] * self.dimensions
        for feature, weight in features.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self.dimensions
            vector[index] += weight

        norm = math.sqrt(math.fsum(value * value for value in vector))
        if norm:
            vector = [value / norm for value in vector]
        return tuple(vector)


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return cosine similarity for compatible dense vectors."""
    if len(left) != len(right):
        raise ValueError("vectors must have the same dimensions")
    left_norm = math.sqrt(math.fsum(float(value) ** 2 for value in left))
    right_norm = math.sqrt(math.fsum(float(value) ** 2 for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    dot = math.fsum(float(a) * float(b) for a, b in zip(left, right))
    return dot / (left_norm * right_norm)


T = TypeVar("T")
K = TypeVar("K", bound=Hashable)


def reciprocal_rank_fusion(
    lexical_ranked: Sequence[T],
    semantic_ranked: Sequence[T],
    *,
    key: Callable[[T], K],
    semantic_weight: float = 0.35,
    rrf_k: int = 60,
) -> list[T]:
    """Fuse ranked candidate lists using weighted reciprocal rank fusion.

    Each list contributes ``weight / (rrf_k + one_based_rank)``. The returned
    order uses the fused score and then the stable key as a deterministic tie
    breaker; raw scores are intentionally not included in evidence objects.
    """
    if not 0.0 <= semantic_weight <= 1.0:
        raise ValueError("semantic_weight must be between 0 and 1")
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")

    scores: dict[K, float] = {}
    items: dict[K, T] = {}
    lexical_weight = 1.0 - semantic_weight
    if lexical_weight:
        for rank, item in enumerate(lexical_ranked, start=1):
            item_key = key(item)
            items.setdefault(item_key, item)
            scores[item_key] = scores.get(item_key, 0.0) + lexical_weight / (rrf_k + rank)
    if semantic_weight:
        for rank, item in enumerate(semantic_ranked, start=1):
            item_key = key(item)
            items.setdefault(item_key, item)
            scores[item_key] = scores.get(item_key, 0.0) + semantic_weight / (rrf_k + rank)

    return sorted(items.values(), key=lambda item: (-scores[key(item)], str(key(item))))
