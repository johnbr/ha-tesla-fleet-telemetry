"""Tesla Schnorr/P-256 signing and ``Tesla.SS256`` JWT assembly.

Implements the signing scheme Tesla requires for the
``fleet_telemetry_config_jws`` endpoint. Tesla invented their own JWT
algorithm — ``Tesla.SS256`` — which is RFC 8235 Schnorr signatures over
NIST P-256 with SHA-256, plus RFC 6979 deterministic nonces. None of the
standard Python JWT libraries support this, so we implement it directly.

Reference (Go): teslamotors/vehicle-command @ main
  internal/schnorr/{sign,schnorr}.go
  internal/authentication/jwt.go
  pkg/sign/sign.go

Wire compatibility is locked down by two known-answer tests in the unit
test suite — RFC 6979 §A.2.5's standard nonce vector and Tesla's own
``goodSig()`` from ``schnorr_test.go``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

# NIST P-256 subgroup order (curve order n).
_P256_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551

# 65-byte uncompressed encoding of the P-256 generator G. Derived once
# from cryptography rather than hard-coded so a typo can't slip in.
_G_UNCOMPRESSED: bytes = (
    ec.derive_private_key(1, ec.SECP256R1())
    .public_key()
    .public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
)
assert len(_G_UNCOMPRESSED) == 65 and _G_UNCOMPRESSED[0] == 0x04


def _scalar_to_uncompressed_pub(scalar: int) -> bytes:
    """Compute scalar·G as a 65-byte uncompressed SEC1 point."""
    if not (1 <= scalar < _P256_N):
        raise ValueError("scalar out of range [1, n-1]")
    return (
        ec.derive_private_key(scalar, ec.SECP256R1())
        .public_key()
        .public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
    )


def _rfc6979_p256_sha256_nonce(scalar_bytes: bytes, digest: bytes) -> bytes:
    """Deterministic nonce per RFC 6979 §3.2, specialised to P-256 + SHA-256.

    Mirrors ``internal/schnorr/sign.go:DeterministicNonce``. Step labels
    in the comments match the RFC for readability.
    """
    if len(scalar_bytes) != 32 or len(digest) != 32:
        raise ValueError("scalar and digest must each be 32 bytes")

    # Step (b): V = 0x01 0x01 ... 0x01
    V = b"\x01" * 32
    # Step (c): K = 0x00 0x00 ... 0x00
    K = b"\x00" * 32

    # bits2octets(digest) for steps (d) and (f). hlen == qlen == 256, so
    # this is just digest reduced mod n.
    h1 = (int.from_bytes(digest, "big") % _P256_N).to_bytes(32, "big")

    # Step (d): K = HMAC_K(V || 0x00 || scalar || h1)
    K = hmac.new(K, V + b"\x00" + scalar_bytes + h1, hashlib.sha256).digest()
    # Step (e): V = HMAC_K(V)
    V = hmac.new(K, V, hashlib.sha256).digest()
    # Step (f): K = HMAC_K(V || 0x01 || scalar || h1)
    K = hmac.new(K, V + b"\x01" + scalar_bytes + h1, hashlib.sha256).digest()
    # Step (g): V = HMAC_K(V)
    V = hmac.new(K, V, hashlib.sha256).digest()

    # Step (h): rejection-sampling loop. With hlen == qlen we don't need
    # an inner concatenation step — V itself is the candidate.
    while True:
        V = hmac.new(K, V, hashlib.sha256).digest()
        candidate = int.from_bytes(V, "big")
        if 1 <= candidate < _P256_N:
            return V
        # Reject this V; mix in 0x00 and try again.
        K = hmac.new(K, V + b"\x00", hashlib.sha256).digest()
        V = hmac.new(K, V, hashlib.sha256).digest()


def _challenge(public_nonce: bytes, sender_pub: bytes, message: bytes) -> bytes:
    """Tesla's Schnorr challenge hash:

        SHA256( LV(G) || LV(V) || LV(A) || LV(m) )

    where LV(x) prefixes ``x`` with its length as a 4-byte big-endian
    integer. ``G`` is the curve generator (uncompressed). Mirrors
    ``internal/schnorr/schnorr.go:challenge``.
    """
    h = hashlib.sha256()
    for buf in (_G_UNCOMPRESSED, public_nonce, sender_pub, message):
        h.update(len(buf).to_bytes(4, "big"))
        h.update(buf)
    return h.digest()


def schnorr_sign(scalar_bytes: bytes, message: bytes) -> bytes:
    """Sign ``message`` with the P-256 private scalar; returns the 96-byte
    Tesla-format Schnorr signature ``V_x || V_y || r``.
    """
    if len(scalar_bytes) != 32:
        raise ValueError("scalar must be 32 bytes (big-endian)")

    digest = hashlib.sha256(message).digest()
    nonce_bytes = _rfc6979_p256_sha256_nonce(scalar_bytes, digest)
    nonce_int = int.from_bytes(nonce_bytes, "big")

    public_nonce = _scalar_to_uncompressed_pub(nonce_int)
    sender_pub = _scalar_to_uncompressed_pub(int.from_bytes(scalar_bytes, "big"))

    c = int.from_bytes(_challenge(public_nonce, sender_pub, message), "big")
    a = int.from_bytes(scalar_bytes, "big")
    r = (nonce_int - a * c) % _P256_N

    # 96 bytes: V_x || V_y (drop the 0x04 prefix) || r
    return public_nonce[1:] + r.to_bytes(32, "big")


# ---------------------------------------------------------------------
# JWT assembly
# ---------------------------------------------------------------------
TESLA_SS256_ALG = "Tesla.SS256"
TELEMETRY_AUDIENCE = "com.tesla.fleet.TelemetryClient"


def _b64url(data: bytes) -> str:
    """Base64url without padding (per JWT RFC 7515)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _load_partner_private_scalar(pem: str) -> tuple[int, bytes]:
    """Parse a partner private-key PEM. Returns (scalar_int, pub_uncompressed_bytes)."""
    key = serialization.load_pem_private_key(pem.encode(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("partner key is not an EC private key")
    if key.curve.name != "secp256r1":
        raise ValueError(
            f"partner key must be on secp256r1 (P-256); got {key.curve.name}"
        )
    scalar_int = key.private_numbers().private_value
    pub_uncompressed = key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return scalar_int, pub_uncompressed


def sign_telemetry_config_jwt(private_pem: str, config_claims: dict[str, Any]) -> str:
    """Build a ``Tesla.SS256``-signed JWT containing the telemetry config.

    The body posted to ``fleet_telemetry_config_jws`` is::

        {"vins": [...], "token": "<this-jwt>"}

    Tesla's ``SignMessageForFleet`` always overwrites the ``iss`` and
    ``aud`` claims; we mirror that behaviour to avoid surprises.
    """
    scalar_int, pub_uncompressed = _load_partner_private_scalar(private_pem)
    scalar_bytes = scalar_int.to_bytes(32, "big")

    claims = {
        **config_claims,
        # iss = std-base64 (with padding) of the uncompressed public key
        "iss": base64.standard_b64encode(pub_uncompressed).decode("ascii"),
        "aud": TELEMETRY_AUDIENCE,
    }

    header = {"alg": TESLA_SS256_ALG, "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode())
    claims_b64 = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header_b64}.{claims_b64}".encode("ascii")

    signature = schnorr_sign(scalar_bytes, signing_input)
    return f"{header_b64}.{claims_b64}.{_b64url(signature)}"
