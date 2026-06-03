"""Persistent PQ key management for switchboard.

This module layers durable storage and key IDs on top of ``switchboard.pq``.
It is import-safe without ``liboqs`` installed: loading and saving existing
keys works without PQ runtime support, while ``generate()`` / ``sign()`` /
``verify()`` defer to ``switchboard.pq`` and raise there when liboqs is absent.
"""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from . import pq

__all__ = ["PQKeyPair", "verify"]

_PEM_BEGIN = "-----BEGIN SWITCHBOARD PQ KEY-----"
_PEM_END = "-----END SWITCHBOARD PQ KEY-----"
_PUB_BEGIN = "-----BEGIN SWITCHBOARD PQ PUBLIC KEY-----"
_PUB_END = "-----END SWITCHBOARD PQ PUBLIC KEY-----"
_SCRYPT_N = 32768
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12


def _require_crypto():
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise RuntimeError(
            "cryptography is required for encrypted PQ key storage"
        ) from exc
    return ChaCha20Poly1305


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


def _derive_key(passphrase: bytes, salt: bytes, n: int, r: int, p: int) -> bytes:
    try:
        from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

        return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(passphrase)
    except ImportError:  # pragma: no cover - env-dependent fallback
        return hashlib.scrypt(passphrase, salt=salt, n=n, r=r, p=p, dklen=32, maxmem=0)


def _key_id(pk: bytes) -> str:
    return hashlib.sha256(pk).digest()[:16].hex()


def _parse_pem_lines(path: Path, begin: str, end: str) -> tuple[dict[str, str], str]:
    lines = path.read_text().splitlines()
    if not lines or lines[0].strip() != begin or lines[-1].strip() != end:
        raise ValueError(f"invalid key envelope in {path}")
    headers: dict[str, str] = {}
    payload_lines: list[str] = []
    for line in lines[1:-1]:
        if ": " in line and not payload_lines:
            k, v = line.split(": ", 1)
            headers[k.strip()] = v.strip()
        elif line.strip():
            payload_lines.append(line.strip())
    if not payload_lines:
        raise ValueError(f"missing key payload in {path}")
    return headers, "".join(payload_lines)


@dataclass(slots=True)
class PQKeyPair:
    alg: str
    sk: bytes
    pk: bytes
    key_id: str = field(init=False)

    DEFAULT_ALG: ClassVar[str] = "ml-dsa-65"

    def __post_init__(self) -> None:
        if not isinstance(self.sk, bytes) or not isinstance(self.pk, bytes):
            raise TypeError("sk and pk must be raw bytes")
        pq._check_alg(self.alg)
        self.key_id = _key_id(self.pk)

    @classmethod
    def generate(cls, alg: str = DEFAULT_ALG) -> "PQKeyPair":
        pk, sk = pq.generate(alg)
        return cls(alg=alg, sk=sk, pk=pk)

    @classmethod
    def load(cls, path: str, passphrase: bytes | None = None) -> "PQKeyPair":
        key_path = Path(path)
        headers, payload = _parse_pem_lines(key_path, _PEM_BEGIN, _PEM_END)
        alg = headers["alg"]
        pq._check_alg(alg)

        cipher = headers.get("cipher")
        raw = _b64d(payload)

        if cipher != "chacha20-poly1305" or headers.get("kdf") != "scrypt":
            raise ValueError("unsupported PQ key envelope")
        passphrase = passphrase or b""
        salt = bytes.fromhex(headers["kdf-salt"])
        nonce = bytes.fromhex(headers["cipher-nonce"])
        tag = bytes.fromhex(headers["cipher-tag"])
        n = int(headers["kdf-n"])
        r = int(headers["kdf-r"])
        p = int(headers["kdf-p"])
        key = _derive_key(passphrase, salt, n, r, p)
        ChaCha20Poly1305 = _require_crypto()
        try:
            sk = ChaCha20Poly1305(key).decrypt(nonce, raw + tag, None)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("invalid passphrase or corrupted PQ key file") from exc

        pub_headers, pub_payload = _parse_pem_lines(key_path.with_suffix(key_path.suffix + ".pub"), _PUB_BEGIN, _PUB_END)
        if pub_headers.get("alg") != alg:
            raise ValueError("public key algorithm does not match private key")
        pk = _b64d(pub_payload)
        key = cls(alg=alg, sk=sk, pk=pk)
        expected_key_id = pub_headers.get("key-id")
        if expected_key_id and expected_key_id != key.key_id:
            raise ValueError("public key key-id does not match contents")
        return key

    def save(self, path: str, passphrase: bytes | None = None) -> None:
        key_path = Path(path)
        key_path.parent.mkdir(parents=True, exist_ok=True)

        passphrase = passphrase or b""
        salt = os.urandom(_SALT_BYTES)
        nonce = os.urandom(_NONCE_BYTES)
        key = _derive_key(passphrase, salt, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P)
        ChaCha20Poly1305 = _require_crypto()
        sealed = ChaCha20Poly1305(key).encrypt(nonce, self.sk, None)
        payload, tag = sealed[:-16], sealed[-16:]
        headers = {
            "alg": self.alg,
            "kdf": "scrypt",
            "kdf-salt": salt.hex(),
            "kdf-n": str(_SCRYPT_N),
            "kdf-r": str(_SCRYPT_R),
            "kdf-p": str(_SCRYPT_P),
            "cipher": "chacha20-poly1305",
            "cipher-nonce": nonce.hex(),
            "cipher-tag": tag.hex(),
        }

        key_text = "\n".join(
            [_PEM_BEGIN]
            + [f"{k}: {v}" for k, v in headers.items()]
            + [_b64e(payload), _PEM_END, ""]
        )
        key_path.write_text(key_text)

        pub_text = "\n".join(
            [
                _PUB_BEGIN,
                f"alg: {self.alg}",
                f"key-id: {self.key_id}",
                _b64e(self.pk),
                _PUB_END,
                "",
            ]
        )
        key_path.with_suffix(key_path.suffix + ".pub").write_text(pub_text)

    def sign(self, transcript: bytes) -> bytes:
        return pq.sign(self.alg, self.sk, transcript)


def verify(alg: str, pk: bytes, transcript: bytes, sig: bytes) -> bool:
    return pq.verify(alg, pk, transcript, sig)
