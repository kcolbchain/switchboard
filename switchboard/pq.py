"""Post-quantum signature primitives for switchboard.

A thin, opinionated wrapper around `liboqs-python <https://github.com/open-quantum-safe/liboqs-python>`_
exposing exactly what the rest of switchboard needs to sign payment
envelopes per the post-quantum RFC (``docs/post-quantum.md``).

The public surface is intentionally narrow::

    SUPPORTED_ALGS        # frozenset of canonical names

    PQNotAvailable        # raised when liboqs is missing
    UnknownAlgorithm      # raised on unrecognized canonical names

    generate(alg)         -> (pk, sk)
    sign(alg, sk, msg)    -> sig
    verify(alg, pk, msg, sig) -> bool
    sig_size(alg)         -> int   (bytes)
    pk_size(alg)          -> int   (bytes)

Algorithm names follow the FIPS-204 / FIPS-205 canonical strings used
in `docs/post-quantum.md` §3.1:

- ``"ml-dsa-44"``, ``"ml-dsa-65"``, ``"ml-dsa-87"``  (FIPS 204)
- ``"slh-dsa-128s"``, ``"slh-dsa-128f"``             (FIPS 205)

Internally these are mapped to the names ``liboqs`` exposes at runtime.
Older liboqs releases still use the pre-standardization names
(``Dilithium2``, ``SPHINCS+-SHA2-128s-simple``); newer ones expose the
FIPS names directly. The mapping tries the FIPS name first and falls
back to the legacy name so the wrapper works against any liboqs
version that still ships these algorithms.

This module is import-safe without liboqs installed — the import-time
guard sets ``HAS_OQS = False`` and every call that would require
liboqs raises ``PQNotAvailable``. Install via the ``[pq]`` optional
extra::

    pip install 'switchboard-agents[pq]'

Reference: ``docs/post-quantum.md`` §3 (wire format), §4 (key
management), §10 (sub-issue PQ-2).
"""

from __future__ import annotations

from typing import Tuple


try:
    import oqs  # type: ignore[import-not-found]

    HAS_OQS = True
# liboqs-python (the binding) raises SystemExit(1) at import time when its
# helper can't fetch or build the native liboqs C library. We treat that
# the same as "package not installed" — wrapper degrades to PQNotAvailable
# on every call rather than crashing the importer.
except (ImportError, SystemExit, OSError):  # pragma: no cover — environment-gated
    HAS_OQS = False
    oqs = None  # type: ignore[assignment]


__all__ = [
    "HAS_OQS",
    "SUPPORTED_ALGS",
    "PQNotAvailable",
    "UnknownAlgorithm",
    "generate",
    "sign",
    "verify",
    "sig_size",
    "pk_size",
]


SUPPORTED_ALGS: frozenset[str] = frozenset({
    "ml-dsa-44",
    "ml-dsa-65",
    "ml-dsa-87",
    "slh-dsa-128s",
    "slh-dsa-128f",
})


# Canonical → list of liboqs names to try, in order. The FIPS name goes
# first; the legacy name is the fallback so this still works against
# older liboqs releases.
_LIBOQS_NAMES: dict[str, tuple[str, ...]] = {
    "ml-dsa-44":    ("ML-DSA-44",      "Dilithium2"),
    "ml-dsa-65":    ("ML-DSA-65",      "Dilithium3"),
    "ml-dsa-87":    ("ML-DSA-87",      "Dilithium5"),
    "slh-dsa-128s": ("SLH-DSA-SHA2-128s", "SPHINCS+-SHA2-128s-simple"),
    "slh-dsa-128f": ("SLH-DSA-SHA2-128f", "SPHINCS+-SHA2-128f-simple"),
}


class PQNotAvailable(RuntimeError):
    """liboqs-python is not installed; PQ primitives unavailable."""


class UnknownAlgorithm(ValueError):
    """An algorithm name was passed that isn't in SUPPORTED_ALGS."""


def _require_oqs() -> None:
    if not HAS_OQS:
        raise PQNotAvailable(
            "liboqs-python is not installed. "
            "Install via: pip install 'switchboard-agents[pq]'"
        )


def _check_alg(alg: str) -> None:
    if alg not in SUPPORTED_ALGS:
        raise UnknownAlgorithm(
            f"algorithm {alg!r} not in SUPPORTED_ALGS={sorted(SUPPORTED_ALGS)!r}"
        )


def _oqs_signature(alg: str, secret_key: bytes | None = None):
    """Open a fresh ``oqs.Signature`` for ``alg``.

    Tries each candidate name from ``_LIBOQS_NAMES`` until one
    constructs successfully, so the same canonical name works across
    liboqs releases that pre- and post-date the FIPS rename. Raises
    ``PQNotAvailable`` if every candidate fails to instantiate (i.e.
    the installed liboqs build didn't include this algorithm).
    """
    last_err: Exception | None = None
    for name in _LIBOQS_NAMES[alg]:
        try:
            if secret_key is None:
                return oqs.Signature(name)
            return oqs.Signature(name, secret_key=secret_key)
        except Exception as exc:  # noqa: BLE001 — oqs raises a mix of types
            last_err = exc
            continue
    raise PQNotAvailable(
        f"liboqs has no enabled algorithm matching {alg!r} "
        f"(tried {list(_LIBOQS_NAMES[alg])}). "
        f"Last error: {last_err!r}"
    )


def generate(alg: str) -> Tuple[bytes, bytes]:
    """Generate a fresh ``(public_key, secret_key)`` pair for ``alg``.

    Returns the keys as raw ``bytes`` so the caller owns storage — we
    do not retain any state across the call.
    """
    _require_oqs()
    _check_alg(alg)
    with _oqs_signature(alg) as signer:
        pk = bytes(signer.generate_keypair())
        sk = bytes(signer.export_secret_key())
        return pk, sk


def sign(alg: str, sk: bytes, msg: bytes) -> bytes:
    """Sign ``msg`` with ``sk`` under ``alg``. Returns the signature bytes."""
    _require_oqs()
    _check_alg(alg)
    with _oqs_signature(alg, secret_key=sk) as signer:
        return bytes(signer.sign(msg))


def verify(alg: str, pk: bytes, msg: bytes, sig: bytes) -> bool:
    """Return ``True`` iff ``sig`` is a valid ``alg`` signature on ``msg`` under ``pk``."""
    _require_oqs()
    _check_alg(alg)
    with _oqs_signature(alg) as verifier:
        try:
            return bool(verifier.verify(msg, sig, pk))
        except Exception:
            # liboqs raises on truncated/garbage inputs; treat as "invalid".
            return False


def sig_size(alg: str) -> int:
    """Return the canonical signature length in bytes for ``alg``."""
    _require_oqs()
    _check_alg(alg)
    with _oqs_signature(alg) as s:
        return int(s.details["length_signature"])


def pk_size(alg: str) -> int:
    """Return the canonical public-key length in bytes for ``alg``."""
    _require_oqs()
    _check_alg(alg)
    with _oqs_signature(alg) as s:
        return int(s.details["length_public_key"])
