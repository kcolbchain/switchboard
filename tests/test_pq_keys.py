from __future__ import annotations

import importlib

import pytest

from switchboard import pq
from switchboard import pq_keys


def test_load_save_roundtrip_with_passphrase(tmp_path) -> None:
    pair = pq_keys.PQKeyPair(alg="ml-dsa-65", sk=b"secret-key-material", pk=b"public-key-material")
    path = tmp_path / "agent-pq.key"

    pair.save(str(path), passphrase=b"correct horse battery staple")
    loaded = pq_keys.PQKeyPair.load(str(path), passphrase=b"correct horse battery staple")

    assert loaded.alg == pair.alg
    assert loaded.sk == pair.sk
    assert loaded.pk == pair.pk
    assert loaded.key_id == pair.key_id
    assert path.with_suffix(path.suffix + ".pub").exists()


def test_wrong_passphrase_fails_loudly(tmp_path) -> None:
    pair = pq_keys.PQKeyPair(alg="ml-dsa-65", sk=b"secret-key-material", pk=b"public-key-material")
    path = tmp_path / "agent-pq.key"
    pair.save(str(path), passphrase=b"right-passphrase")

    with pytest.raises(ValueError, match="invalid passphrase|corrupted"):
        pq_keys.PQKeyPair.load(str(path), passphrase=b"wrong-passphrase")


def test_key_id_is_deterministic() -> None:
    pk = b"same-public-key"
    a = pq_keys.PQKeyPair(alg="ml-dsa-65", sk=b"one", pk=pk)
    b = pq_keys.PQKeyPair(alg="ml-dsa-65", sk=b"two", pk=pk)

    assert a.key_id == b.key_id
    assert len(a.key_id) == 32


def test_generate_uses_pq_module(monkeypatch) -> None:
    def fake_generate(alg: str):
        assert alg == "ml-dsa-44"
        return (b"pub", b"sec")

    monkeypatch.setattr(pq_keys.pq, "generate", fake_generate)
    pair = pq_keys.PQKeyPair.generate("ml-dsa-44")
    assert pair.pk == b"pub"
    assert pair.sk == b"sec"
    assert pair.alg == "ml-dsa-44"


def test_sign_and_verify_delegate(monkeypatch) -> None:
    pair = pq_keys.PQKeyPair(alg="ml-dsa-65", sk=b"sec", pk=b"pub")
    calls = []

    def fake_sign(alg: str, sk: bytes, transcript: bytes) -> bytes:
        calls.append(("sign", alg, sk, transcript))
        return b"sig"

    def fake_verify(alg: str, pk: bytes, transcript: bytes, sig: bytes) -> bool:
        calls.append(("verify", alg, pk, transcript, sig))
        return True

    monkeypatch.setattr(pq_keys.pq, "sign", fake_sign)
    monkeypatch.setattr(pq_keys.pq, "verify", fake_verify)

    sig = pair.sign(b"transcript")
    ok = pq_keys.verify(pair.alg, pair.pk, b"transcript", sig)

    assert sig == b"sig"
    assert ok is True
    assert calls == [
        ("sign", "ml-dsa-65", b"sec", b"transcript"),
        ("verify", "ml-dsa-65", b"pub", b"transcript", b"sig"),
    ]


def test_save_load_without_passphrase_still_uses_envelope(tmp_path) -> None:
    pair = pq_keys.PQKeyPair(alg="ml-dsa-65", sk=b"secret-key-material", pk=b"public-key-material")
    path = tmp_path / "agent-pq.key"

    pair.save(str(path))
    loaded = pq_keys.PQKeyPair.load(str(path))

    assert loaded.sk == pair.sk
    assert loaded.pk == pair.pk
    text = path.read_text()
    assert "kdf: scrypt" in text
    assert "cipher: chacha20-poly1305" in text
    assert "cipher-tag:" in text


def test_import_safe_without_oqs(monkeypatch) -> None:
    monkeypatch.setattr(pq, "HAS_OQS", False)
    reloaded = importlib.reload(pq_keys)
    pair = reloaded.PQKeyPair(alg="ml-dsa-65", sk=b"secret", pk=b"public")
    assert hasattr(reloaded, "PQKeyPair")
    assert hasattr(reloaded, "verify")
    assert pair.key_id
