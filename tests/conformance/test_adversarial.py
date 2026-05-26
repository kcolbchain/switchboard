import json, os
from pathlib import Path


def load_vectors():
    path = Path(__file__).parent / "adversarial_vectors.json"
    with open(path) as f:
        return json.load(f)


def test_all_vectors():
    vectors = load_vectors()
    assert "vectors" in vectors
    for v in vectors["vectors"]:
        assert "id" in v
        assert "input" in v
        assert "expected_error" in v


def test_malformed_payload():
    v = load_vectors()["vectors"][0]
    assert v["id"] == "malformed-payload"
