"""Ed25519 signatures over a receipt's canonical SHA-256 hash.

What is signed: the ASCII bytes of the 64-character lowercase hex digest that
`receipts.receipt.canonical_hash` produces (and stores in the receipt's `sha256` field).
A third party verifies a receipt in two steps:
  1. recompute canonical_hash(receipt) and compare it to receipt["sha256"]  (content integrity)
  2. Ed25519-verify the signature over that hex string with the public key  (it came from us)
"""
from __future__ import annotations

import base64
import hashlib
import logging
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from receipts.receipt import canonical_hash

from .config import ServiceSettings

log = logging.getLogger("receipts.service.signing")
ALG = "Ed25519"


class Signer:
    def __init__(self, private_key: Ed25519PrivateKey):
        self._sk = private_key
        self.public_key = private_key.public_key()
        raw = self.public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_raw_b64 = base64.b64encode(raw).decode()
        self.key_id = hashlib.sha256(raw).hexdigest()[:16]

    @property
    def public_pem(self) -> str:
        return self.public_key.public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()

    def sign_hash(self, sha256_hex: str) -> str:
        return base64.b64encode(self._sk.sign(sha256_hex.encode("ascii"))).decode()

    def jwk(self) -> dict:
        raw = base64.b64decode(self.public_raw_b64)
        return {"kty": "OKP", "crv": "Ed25519", "kid": self.key_id,
                "x": base64.urlsafe_b64encode(raw).decode().rstrip("=")}


def _load_private(material: str) -> Ed25519PrivateKey:
    material = material.strip()
    if material.startswith("-----BEGIN"):
        k = serialization.load_pem_private_key(material.encode(), password=None)
        if not isinstance(k, Ed25519PrivateKey):
            raise ValueError("signing key is not an Ed25519 key")
        return k
    raw = base64.b64decode(material)
    if len(raw) != 32:
        raise ValueError("SERVICE_SIGNING_KEY must be a PEM or base64 of a 32-byte Ed25519 seed")
    return Ed25519PrivateKey.from_private_bytes(raw)


def generate_pem() -> str:
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()


def load_signer(s: ServiceSettings, create: bool = True) -> Signer:
    """Key order: SERVICE_SIGNING_KEY, SERVICE_SIGNING_KEY_FILE, <data_dir>/signing_key.pem (generated in dev)."""
    if s.signing_key:
        return Signer(_load_private(s.signing_key))
    path = Path(s.signing_key_file) if s.signing_key_file else Path(s.data_dir) / "signing_key.pem"
    if not path.exists():
        if not create:
            raise FileNotFoundError(f"no signing key at {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(generate_pem(), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass
        log.warning("generated a new Ed25519 signing key at %s (set SERVICE_SIGNING_KEY[_FILE] in production)", path)
    return Signer(_load_private(path.read_text(encoding="utf-8")))


def verify(receipt: dict, signature_b64: str | None, public_key: Ed25519PublicKey) -> dict:
    """Checks content integrity and the signature. Never raises on bad input."""
    out = {"hash_ok": False, "signature_ok": False, "sha256": None, "valid": False}
    if not isinstance(receipt, dict):
        out["error"] = "receipt must be a JSON object"
        return out
    claimed = receipt.get("sha256")
    body = {k: v for k, v in receipt.items() if k != "_paths"}
    recomputed = canonical_hash(body)
    out["sha256"] = recomputed
    out["hash_ok"] = bool(claimed) and claimed == recomputed
    if signature_b64 and claimed:
        try:
            public_key.verify(base64.b64decode(signature_b64), str(claimed).encode("ascii"))
            out["signature_ok"] = True
        except (InvalidSignature, ValueError, TypeError):
            out["signature_ok"] = False
    out["valid"] = out["hash_ok"] and out["signature_ok"]
    return out
