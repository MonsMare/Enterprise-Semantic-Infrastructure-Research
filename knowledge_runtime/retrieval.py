"""Deterministic local retrieval primitives for hybrid chunk ranking.

The bundled encoder is a hashed word/character n-gram lexical proxy. It is not
a neural embedding model and performs no network or cloud calls. Providers can
inject another encoder implementing :class:`SemanticEncoder`.
"""

from __future__ import annotations

import hashlib
import math
import re
import struct
from array import array
from collections import Counter
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeVar


@dataclass(frozen=True)
class SparseVector:
    """A normalized fixed-dimension vector with compact sorted coordinates."""

    dimensions: int
    indices: array
    values: array
    norm: float

    def __post_init__(self) -> None:
        if self.dimensions <= 0:
            raise ValueError("dimensions must be positive")
        if self.indices.typecode != "I" or self.values.typecode != "d":
            raise ValueError("sparse coordinates must use unsigned-int indices and float64 values")
        if len(self.indices) != len(self.values):
            raise ValueError("sparse coordinate and value counts must match")
        if any(index < 0 or index >= self.dimensions for index in self.indices):
            raise ValueError("sparse vector coordinate is out of range")

    @classmethod
    def from_mapping(cls, dimensions: int, values: dict[int, float], norm: float) -> "SparseVector":
        coordinates = sorted(values.items())
        return cls(
            dimensions=dimensions,
            indices=array("I", (index for index, _value in coordinates)),
            values=array("d", (value for _index, value in coordinates)),
            norm=norm,
        )

    def to_blob(self) -> bytes:
        """Serialize sparse coordinates in a compact, platform-independent form."""
        return b"".join(
            struct.pack("<Id", index, value)
            for index, value in zip(self.indices, self.values)
        )

    @classmethod
    def from_blob(cls, dimensions: int, value: bytes, norm: float) -> "SparseVector":
        if len(value) % 12:
            raise ValueError("sparse vector blob has an invalid length")
        coordinates = list(struct.iter_unpack("<Id", value))
        return cls.from_mapping(dimensions, dict(coordinates), norm)


class SemanticEncoder(Protocol):
    """Interface for local or remote semantic encoders with vector output."""

    def encode(self, text: str) -> Sequence[float] | SparseVector: ...


class LocalNgramEncoder:
    """Encode text with deterministic hashed word and character n-grams."""

    def __init__(self, dimensions: int = 2048) -> None:
        if dimensions <= 0:
            raise ValueError("dimensions must be positive")
        self.dimensions = dimensions
        self.encoder_id = f"local-ngram-v1:{dimensions}"

    def encode(self, text: str) -> SparseVector:
        words = re.findall(r"\w+", text.casefold())
        features: Counter[str] = Counter()
        for word in words:
            features[f"w:{word}"] += 2.0
            padded = f"^{word}$"
            for size in range(2, min(4, len(padded)) + 1):
                for index in range(len(padded) - size + 1):
                    features[f"c{size}:{padded[index:index + size]}"] += 0.5

        vector: dict[int, float] = {}
        for feature, weight in features.items():
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self.dimensions
            vector[index] = vector.get(index, 0.0) + weight

        norm = math.sqrt(math.fsum(value * value for value in vector.values()))
        if norm:
            vector = {index: value / norm for index, value in vector.items()}
        return SparseVector.from_mapping(self.dimensions, vector, 1.0 if norm else 0.0)


def cosine_similarity(
    left: Sequence[float] | SparseVector,
    right: Sequence[float] | SparseVector,
) -> float:
    """Return cosine similarity for compatible dense or sparse vectors."""
    if isinstance(left, SparseVector) and isinstance(right, SparseVector):
        if left.dimensions != right.dimensions:
            raise ValueError("vectors must have the same dimensions")
        if left.norm == 0.0 or right.norm == 0.0:
            return 0.0
        left_index = right_index = 0
        products: list[float] = []
        while left_index < len(left.indices) and right_index < len(right.indices):
            left_coordinate = left.indices[left_index]
            right_coordinate = right.indices[right_index]
            if left_coordinate == right_coordinate:
                products.append(left.values[left_index] * right.values[right_index])
                left_index += 1
                right_index += 1
            elif left_coordinate < right_coordinate:
                left_index += 1
            else:
                right_index += 1
        dot = math.fsum(products)
        return dot / (left.norm * right.norm)

    if isinstance(left, SparseVector) or isinstance(right, SparseVector):
        sparse, dense = (left, right) if isinstance(left, SparseVector) else (right, left)
        if len(dense) != sparse.dimensions:
            raise ValueError("vectors must have the same dimensions")
        sparse_norm = sparse.norm
        dense_norm = math.sqrt(math.fsum(float(value) ** 2 for value in dense))
        if sparse_norm == 0.0 or dense_norm == 0.0:
            return 0.0
        dot = math.fsum(
            value * float(dense[index])
            for index, value in zip(sparse.indices, sparse.values)
        )
        return dot / (sparse_norm * dense_norm)

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
