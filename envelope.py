"""AES-256-GCM envelope for the web API (design §2.1): base64(nonce[12] || ciphertext+tag), aad b"dp1"."""
import base64
import binascii
import json
import os
import time

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

AAD = b"dp1"
MAX_SKEW = 60
REPLAY_WINDOW = 120


class Rejected(Exception):
    """Anything that must get a bare HTTP 400."""


def load_keys(*b64_keys: str | None) -> list[AESGCM]:
    keys = []
    for k in b64_keys:
        if k and k.strip():
            raw = base64.b64decode(k.strip())
            if len(raw) != 32:
                raise ValueError("WEB_API_KEY must be 32 bytes, base64")
            keys.append(AESGCM(raw))
    return keys


def seal(key: AESGCM, obj, nonce: bytes | None = None) -> str:
    nonce = nonce or os.urandom(12)
    data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()
    return base64.b64encode(nonce + key.encrypt(nonce, data, AAD)).decode()


def open_envelope(keys: list[AESGCM], body: bytes) -> tuple[AESGCM, bytes]:
    """Returns the key that authenticated it and the plaintext."""
    try:
        raw = base64.b64decode(body.strip(), validate=True)
    except (binascii.Error, ValueError):
        raise Rejected("base64")
    if len(raw) < 12 + 16:
        raise Rejected("short")
    for key in keys:
        try:
            return key, key.decrypt(raw[:12], raw[12:], AAD)
        except InvalidTag:
            continue
    raise Rejected("auth")


class ReplayGuard:
    """Checks the request fields that make an envelope fresh: v, ts within MAX_SKEW, req_id unseen in REPLAY_WINDOW."""

    def __init__(self):
        self.seen: dict[str, float] = {}

    def check(self, req: dict, now: float | None = None):
        now = time.time() if now is None else now
        ts, req_id = req.get("ts"), req.get("req_id")
        if req.get("v") != 1 or not isinstance(ts, int) or isinstance(ts, bool) or not isinstance(req_id, str) or not req_id:
            raise Rejected("fields")
        if abs(ts - now) > MAX_SKEW:
            raise Rejected("ts")
        self.seen = {r: t for r, t in self.seen.items() if now - t < REPLAY_WINDOW}
        if req_id in self.seen:
            raise Rejected("replay")
        self.seen[req_id] = now


def decode_request(keys: list[AESGCM], guard: ReplayGuard, body: bytes, now: float | None = None) -> tuple[AESGCM, dict]:
    key, plain = open_envelope(keys, body)
    try:
        req = json.loads(plain)
    except ValueError:
        raise Rejected("json")
    if not isinstance(req, dict):
        raise Rejected("json")
    guard.check(req, now)
    return key, req
