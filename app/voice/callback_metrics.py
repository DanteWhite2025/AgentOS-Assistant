"""Process-local, secret-free callback ingress counters."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)


@dataclass
class VoiceCallbackMetrics:
    callback_received_total: int = 0
    callback_accepted_total: int = 0
    callback_duplicate_total: int = 0
    callback_stale_total: int = 0
    callback_schema_rejected_total: int = 0
    callback_signature_rejected_total: int = 0
    callback_first_received_at: datetime | None = None
    user_transcript_at: datetime | None = None
    assistant_first_token_at: datetime | None = None
    tts_first_packet_at: datetime | None = None

    def increment(self, name: str, *, at: datetime | None = None) -> None:
        now = at or datetime.now(UTC)
        if name == "received":
            self.callback_received_total += 1
            self.callback_first_received_at = self.callback_first_received_at or now
        elif name == "accepted":
            self.callback_accepted_total += 1
        elif name == "duplicate":
            self.callback_duplicate_total += 1
        elif name == "stale":
            self.callback_stale_total += 1
        elif name == "schema_rejected":
            self.callback_schema_rejected_total += 1
        elif name == "signature_rejected":
            self.callback_signature_rejected_total += 1
        else:
            raise ValueError(f"unknown callback metric: {name}")
        logger.info(
            "voice_callback_metrics callback_received_total=%s callback_accepted_total=%s "
            "callback_duplicate_total=%s callback_stale_total=%s "
            "callback_schema_rejected_total=%s callback_signature_rejected_total=%s "
            "callback_first_received_at=%s",
            self.callback_received_total,
            self.callback_accepted_total,
            self.callback_duplicate_total,
            self.callback_stale_total,
            self.callback_schema_rejected_total,
            self.callback_signature_rejected_total,
            self.callback_first_received_at.isoformat()
            if self.callback_first_received_at
            else None,
        )

    def observe_pipeline(self, name: str, *, at: datetime) -> None:
        if name == "user_transcript":
            self.user_transcript_at = self.user_transcript_at or at
        elif name == "assistant_first_token":
            self.assistant_first_token_at = self.assistant_first_token_at or at
        elif name == "tts_first_packet":
            self.tts_first_packet_at = self.tts_first_packet_at or at
        else:
            raise ValueError(f"unknown callback pipeline metric: {name}")
