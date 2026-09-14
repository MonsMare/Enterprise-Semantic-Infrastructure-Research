import pytest

from knowledge_runtime.retrieval import LocalNgramEncoder, SparseVector, cosine_similarity


def test_local_ngram_encoder_uses_sparse_normalized_vectors():
    vector = LocalNgramEncoder(dimensions=4096).encode("actuarial reserve estimate")

    assert isinstance(vector, SparseVector)
    assert vector.dimensions == 4096
    assert 0 < len(vector.values) < 100
    assert sum(value * value for value in vector.values) == pytest.approx(1.0)


def test_cosine_similarity_matches_sparse_and_dense_representations():
    encoder = LocalNgramEncoder(dimensions=128)
    left = encoder.encode("annual reserve review")
    right = encoder.encode("reserve review each year")
    left_values = dict(zip(left.indices, left.values))
    right_values = dict(zip(right.indices, right.values))
    dense_left = tuple(left_values.get(index, 0.0) for index in range(left.dimensions))
    dense_right = tuple(right_values.get(index, 0.0) for index in range(right.dimensions))

    assert cosine_similarity(left, right) == pytest.approx(
        cosine_similarity(dense_left, dense_right)
    )
    assert cosine_similarity(left, dense_right) == pytest.approx(
        cosine_similarity(dense_left, dense_right)
    )


def test_cosine_similarity_rejects_sparse_dimension_mismatch():
    encoder = LocalNgramEncoder()

    with pytest.raises(ValueError, match="same dimensions"):
        cosine_similarity(encoder.encode("reserve"), LocalNgramEncoder(128).encode("reserve"))
