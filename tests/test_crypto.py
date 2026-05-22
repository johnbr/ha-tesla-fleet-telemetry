"""Tests for the Tesla.SS256 signer (``crypto.py``).

The wire format is locked down by two known-answer tests:

* the RFC 6979 deterministic-nonce vector from RFC 6979 Appendix A.2.5
  (NIST P-256 + SHA-256), and
* Tesla's own ``goodSig()`` vector from
  ``teslamotors/vehicle-command`` ``internal/schnorr/schnorr_test.go``.

If either KAT fails, the signature this integration posts to
``fleet_telemetry_config_jws`` no longer matches what Tesla accepts.

``crypto.py`` has no Home Assistant imports, so it is loaded directly
from its file path and tested without the HA test harness.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

# --- Load crypto.py in isolation (no package import → no HA dependency) ---
_CRYPTO_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "tesla_telemetry"
    / "crypto.py"
)
_spec = importlib.util.spec_from_file_location("tesla_telemetry_crypto", _CRYPTO_PATH)
crypto = importlib.util.module_from_spec(_spec)
sys.modules["tesla_telemetry_crypto"] = crypto
_spec.loader.exec_module(crypto)


# ---------------------------------------------------------------------------
# KAT 1 — RFC 6979 Appendix A.2.5 (P-256, SHA-256) deterministic nonce
# ---------------------------------------------------------------------------
# Private key x and the deterministic k values for the canonical messages.
_RFC6979_X = bytes.fromhex(
    "C9AFA9D845BA75166B5C215767B1D6934E50C3DB36E89B127B8A622B120F6721"
)
_RFC6979_VECTORS = {
    b"sample": bytes.fromhex(
        "A6E3C57DD01ABE90086538398355DD4C3B17AA873382B0F24D6129493D8AAD60"
    ),
    b"test": bytes.fromhex(
        "D16B6AE827F17175E040871A1C7EC3500192C4C92677336EC2537ACAEE0008E0"
    ),
}


@pytest.mark.parametrize("message,expected_k", _RFC6979_VECTORS.items())
def test_rfc6979_nonce_matches_rfc_vectors(message: bytes, expected_k: bytes) -> None:
    digest = hashlib.sha256(message).digest()
    nonce = crypto._rfc6979_p256_sha256_nonce(_RFC6979_X, digest)
    assert nonce == expected_k


def test_rfc6979_nonce_rejects_wrong_lengths() -> None:
    good = b"\x00" * 32
    with pytest.raises(ValueError):
        crypto._rfc6979_p256_sha256_nonce(b"\x00" * 31, good)
    with pytest.raises(ValueError):
        crypto._rfc6979_p256_sha256_nonce(good, b"\x00" * 33)


# ---------------------------------------------------------------------------
# KAT 2 — Tesla's goodSig() vector from schnorr_test.go
# ---------------------------------------------------------------------------
# testKey(): a 32-byte big-endian scalar whose first byte is 3, rest zero.
_TESLA_SCALAR = bytes([3]) + bytes(31)
_TESLA_MESSAGE = b"hello world"
# The 96-byte signature (V_x || V_y || r) goodSig() returns for that key.
_TESLA_GOODSIG = bytes(
    (
        0x7C, 0xFD, 0xBE, 0xB5, 0xBA, 0xA7, 0x30, 0x54, 0x04, 0x01, 0x55, 0x0B,
        0xDE, 0xFA, 0x20, 0x97, 0x64, 0x53, 0xE8, 0x53, 0x9A, 0xE4, 0xB2, 0xF2,
        0x6C, 0xE3, 0x31, 0x25, 0x80, 0x1A, 0x08, 0xF9, 0x0E, 0xD2, 0x0C, 0x3D,
        0x84, 0x64, 0x97, 0xFF, 0x82, 0xCC, 0x97, 0x72, 0xE3, 0xDB, 0x47, 0x03,
        0x98, 0x2F, 0x47, 0xBD, 0x0B, 0x0B, 0x89, 0xDF, 0xB9, 0xA4, 0x9C, 0xD2,
        0xE5, 0x24, 0x05, 0x46, 0x02, 0xB1, 0xE0, 0x5F, 0xBF, 0x95, 0xF5, 0x68,
        0x6F, 0xAE, 0xA7, 0xA5, 0x80, 0x9E, 0xB9, 0x2F, 0x5E, 0xCC, 0x22, 0xEA,
        0xE7, 0x4C, 0xEC, 0xCC, 0x5E, 0x2A, 0x65, 0xDD, 0x67, 0xFF, 0x20, 0xFC,
    )
)


def test_schnorr_sign_matches_tesla_goodsig() -> None:
    """The full wire-format KAT — output must equal Tesla's reference."""
    assert len(_TESLA_GOODSIG) == 96
    sig = crypto.schnorr_sign(_TESLA_SCALAR, _TESLA_MESSAGE)
    assert sig == _TESLA_GOODSIG


def test_schnorr_sign_is_deterministic() -> None:
    """RFC 6979 nonces make signing reproducible — no randomness."""
    a = crypto.schnorr_sign(_TESLA_SCALAR, b"determinism")
    b = crypto.schnorr_sign(_TESLA_SCALAR, b"determinism")
    assert a == b
    assert len(a) == 96


def test_schnorr_sign_rejects_bad_scalar_length() -> None:
    with pytest.raises(ValueError):
        crypto.schnorr_sign(b"\x03" * 31, _TESLA_MESSAGE)


# ---------------------------------------------------------------------------
# JWT assembly
# ---------------------------------------------------------------------------
def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _p256_pem() -> str:
    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_sign_telemetry_config_jwt_structure() -> None:
    config = {"hostname": "telemetry.example.com", "port": 443, "exp": 1234567890}
    token = crypto.sign_telemetry_config_jwt(_p256_pem(), config)

    header_b64, claims_b64, sig_b64 = token.split(".")

    header = json.loads(_b64url_decode(header_b64))
    assert header == {"alg": crypto.TESLA_SS256_ALG, "typ": "JWT"}

    claims = json.loads(_b64url_decode(claims_b64))
    # Caller-supplied config is preserved...
    for key, value in config.items():
        assert claims[key] == value
    # ...and iss/aud are added.
    assert claims["aud"] == crypto.TELEMETRY_AUDIENCE
    iss = base64.standard_b64decode(claims["iss"])
    assert len(iss) == 65 and iss[0] == 0x04  # uncompressed P-256 point

    assert len(_b64url_decode(sig_b64)) == 96


def test_sign_telemetry_config_jwt_overwrites_iss_and_aud() -> None:
    """Tesla always overwrites iss/aud; a caller can't smuggle their own."""
    token = crypto.sign_telemetry_config_jwt(
        _p256_pem(), {"iss": "attacker", "aud": "attacker"}
    )
    claims = json.loads(_b64url_decode(token.split(".")[1]))
    assert claims["iss"] != "attacker"
    assert claims["aud"] == crypto.TELEMETRY_AUDIENCE


def test_jwt_signature_is_schnorr_sig_of_signing_input() -> None:
    """The JWT's third segment must be schnorr_sign() over header.claims."""
    pem = _p256_pem()
    token = crypto.sign_telemetry_config_jwt(pem, {"hostname": "h", "port": 1})
    header_b64, claims_b64, sig_b64 = token.split(".")

    scalar = (
        serialization.load_pem_private_key(pem.encode(), password=None)
        .private_numbers()
        .private_value
    )
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")
    expected = crypto.schnorr_sign(scalar.to_bytes(32, "big"), signing_input)
    assert _b64url_decode(sig_b64) == expected


def test_sign_telemetry_config_jwt_rejects_non_ec_key() -> None:
    rsa_pem = (
        rsa.generate_private_key(public_exponent=65537, key_size=2048)
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError):
        crypto.sign_telemetry_config_jwt(rsa_pem, {"hostname": "h"})


def test_sign_telemetry_config_jwt_rejects_wrong_curve() -> None:
    p384_pem = (
        ec.generate_private_key(ec.SECP384R1())
        .private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        .decode()
    )
    with pytest.raises(ValueError):
        crypto.sign_telemetry_config_jwt(p384_pem, {"hostname": "h"})
