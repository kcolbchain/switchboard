"""Tests for ``switchboard.pq`` — the liboqs wrapper.

The wrapper is import-safe without liboqs installed, so two kinds of
test live here:

1. **Always-on**: structural assertions about the module — exported
   names, type signatures, raised exceptions in the no-liboqs path.
   These run on any developer machine without ``[pq]`` installed.

2. **Behavioral**: round-trip generate → sign → verify per algorithm,
   plus a tamper-detection negative twin. These are gated by
   ``pytest.importorskip("oqs")`` so the suite stays green when
   liboqs isn't present.
"""

from __future__ import annotations

import pytest

from switchboard import pq


ALL_ALGS = sorted(pq.SUPPORTED_ALGS)


# ─── always-on structural tests ────────────────────────────────────────────


def test_exports() -> None:
    must_have = {
        "HAS_OQS", "SUPPORTED_ALGS",
        "PQNotAvailable", "UnknownAlgorithm",
        "generate", "sign", "verify",
        "sig_size", "pk_size",
    }
    missing = must_have - set(dir(pq))
    assert not missing, f"pq module missing symbols: {missing}"


def test_supported_algs_pin() -> None:
    """The canonical set is frozen — don't add/remove algs without bumping
    a test (and the docs/post-quantum.md algorithm registry)."""
    assert pq.SUPPORTED_ALGS == frozenset({
        "ml-dsa-44", "ml-dsa-65", "ml-dsa-87",
        "slh-dsa-128s", "slh-dsa-128f",
    })


def test_unknown_algorithm_rejected() -> None:
    """Even when liboqs is missing, an unknown alg should raise
    UnknownAlgorithm *before* the missing-liboqs guard fires — easier
    to debug for callers."""
    if pq.HAS_OQS:
        with pytest.raises(pq.UnknownAlgorithm):
            pq.sig_size("not-a-real-algorithm")
    else:
        # Without liboqs, the missing-liboqs guard fires first; that's
        # still a clear error.
        with pytest.raises((pq.PQNotAvailable, pq.UnknownAlgorithm)):
            pq.sig_size("not-a-real-algorithm")


def test_missing_oqs_raises_loudly() -> None:
    """When HAS_OQS is False, every public call should raise
    PQNotAvailable — we don't want silent fallbacks."""
    if pq.HAS_OQS:
        pytest.skip("liboqs is installed; the missing-oqs path doesn't apply")
    with pytest.raises(pq.PQNotAvailable):
        pq.generate("ml-dsa-65")
    with pytest.raises(pq.PQNotAvailable):
        pq.sign("ml-dsa-65", b"\x00" * 32, b"hello")
    with pytest.raises(pq.PQNotAvailable):
        pq.verify("ml-dsa-65", b"\x00" * 32, b"hello", b"\x00" * 64)
    with pytest.raises(pq.PQNotAvailable):
        pq.sig_size("ml-dsa-65")
    with pytest.raises(pq.PQNotAvailable):
        pq.pk_size("ml-dsa-65")


# ─── behavioral tests (need liboqs) ────────────────────────────────────────
# Gate every behavioral test with skipif so structural tests above still
# run on machines without the ``[pq]`` extra installed.

needs_oqs = pytest.mark.skipif(
    not pq.HAS_OQS,
    reason="liboqs-python not installed; install via 'pip install switchboard-agents[pq]'",
)


@needs_oqs
@pytest.mark.parametrize("alg", ALL_ALGS)
def test_roundtrip(alg: str) -> None:
    """Generate → sign → verify should round-trip for every alg."""
    pk, sk = pq.generate(alg)
    assert isinstance(pk, bytes) and isinstance(sk, bytes)
    assert len(pk) > 0 and len(sk) > 0
    sig = pq.sign(alg, sk, b"switchboard test vector")
    assert isinstance(sig, bytes) and len(sig) > 0
    assert pq.verify(alg, pk, b"switchboard test vector", sig) is True


@needs_oqs
@pytest.mark.parametrize("alg", ALL_ALGS)
def test_size_metadata(alg: str) -> None:
    pk, _ = pq.generate(alg)
    assert pq.pk_size(alg) == len(pk)
    assert pq.sig_size(alg) > 0   # SLH-DSA signatures are huge; check non-zero only


@needs_oqs
@pytest.mark.parametrize("alg", ALL_ALGS)
def test_tampered_signature_rejected(alg: str) -> None:
    """Flip one byte of the signature → verify should return False, not raise."""
    pk, sk = pq.generate(alg)
    msg = b"do not modify"
    sig = bytearray(pq.sign(alg, sk, msg))
    # flip a byte near the front so SLH-DSA's structure shifts noticeably
    sig[0] ^= 0xFF
    assert pq.verify(alg, pk, msg, bytes(sig)) is False


@needs_oqs
@pytest.mark.parametrize("alg", ALL_ALGS)
def test_tampered_message_rejected(alg: str) -> None:
    pk, sk = pq.generate(alg)
    sig = pq.sign(alg, sk, b"original message")
    assert pq.verify(alg, pk, b"tampered message", sig) is False


@needs_oqs
@pytest.mark.parametrize("alg", ALL_ALGS)
def test_wrong_pubkey_rejected(alg: str) -> None:
    _, sk = pq.generate(alg)
    pk2, _ = pq.generate(alg)
    sig = pq.sign(alg, sk, b"message")
    assert pq.verify(alg, pk2, b"message", sig) is False


@needs_oqs
def test_verify_returns_false_on_garbage_inputs() -> None:
    """Truncated / random-noise inputs should not raise — return False."""
    pk, _ = pq.generate("ml-dsa-65")
    # Empty sig
    assert pq.verify("ml-dsa-65", pk, b"m", b"") is False
    # Garbage sig of plausible size
    assert pq.verify("ml-dsa-65", pk, b"m", b"\xAA" * pq.sig_size("ml-dsa-65")) is False
