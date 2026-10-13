"""Per-task opaque callback signatures derived from rotatable master secrets."""

from __future__ import annotations

import hashlib
import hmac
from typing import Literal

CALLBACK_AUTH_VERSION = "v1-derived"


def derive_callback_signature(
    master_secret: str,
    *,
    purpose: Literal["state", "function"],
    session_id: str,
    task_id: str,
) -> str:
    message = f"agentos-volc-callback:v1:{purpose}:{session_id}:{task_id}".encode()
    return hmac.new(master_secret.encode(), message, hashlib.sha256).hexdigest()
