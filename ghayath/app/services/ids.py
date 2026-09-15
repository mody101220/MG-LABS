"""Contract ID generation: prefix + Crockford-ULID (matches ^[a-z0-9]+$ DDL patterns)."""
from __future__ import annotations

import os
import time

_CROCKFORD = "0123456789abcdefghjkmnpqrstvwxyz"


def _encode(value: int, length: int) -> str:
    chars = []
    for _ in range(length):
        chars.append(_CROCKFORD[value & 0b11111])
        value >>= 5
    return "".join(reversed(chars))


def new_id(prefix: str) -> str:
    ts = int(time.time() * 1000)
    rand = int.from_bytes(os.urandom(10), "big")
    ulid = _encode(ts, 10) + _encode(rand, 16)
    return f"{prefix}_{ulid}"
