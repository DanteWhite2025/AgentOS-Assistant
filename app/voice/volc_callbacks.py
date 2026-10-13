"""Bounded parsers for Volcengine VoiceChat callbacks.

The conversation-state callback uses a binary envelope documented by Volc:
``message`` is base64(``b"conv" + uint32_be(length) + json``). Function
callbacks use a separate JSON envelope whose ``TaskID`` identifies the
function-calling task, not the VoiceChat task.

Contracts:
- https://www.volcengine.com/docs/6348/1415216?lang=zh
- https://www.volcengine.com/docs/6348/1554654?lang=zh

Only documented containers are inspected. Raw payloads and transcript text
are deliberately not retained or returned by this module.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from dataclasses import dataclass
from typing import Any

MAX_CALLBACK_BYTES = 64 * 1024
_STATE_MAGIC = b"conv"
_BOUNDED_CONTAINERS = (
    "data",
    "Data",
    "event",
    "Event",
    "header",
    "Header",
    "payload",
    "Payload",
    "body",
    "Body",
    "req",
    "Req",
    "ResponseMetadata",
    "response_metadata",
)
_STATE_STAGE_NAMES = {
    0: "error",
    1: "listening",
    2: "thinking",
    3: "answering",
    4: "interrupted",
    5: "answerFinish",
}


class VolcCallbackSchemaError(ValueError):
    """An authenticated callback does not match a supported schema."""


@dataclass(frozen=True)
class StateCallbackEvent:
    signature: str | None
    app_id: str | None
    room_id: str | None
    task_id: str | None
    run_stage: str | None
    error_code: str | None
    error_message: object | None
    event_type: object | None
    provider_trace_id: object | None
    event_time: object | None
    round_id: object | None
    schema_hash: str
    schema_fields: tuple[str, ...]
    schema_kind: str


@dataclass(frozen=True)
class FunctionCallbackEvent:
    signature: str | None
    app_id: str | None
    room_id: str | None
    function_task_id: str | None
    event_type: str | None
    tool_calls: object | None
    notification: dict[str, Any] | None
    schema_hash: str
    schema_fields: tuple[str, ...]


def callback_value(body: dict[str, Any], *names: str) -> object | None:
    """Read only the top level and one documented container level."""

    for name in names:
        if name in body:
            return body[name]
    for container_name in _BOUNDED_CONTAINERS:
        nested = body.get(container_name)
        if not isinstance(nested, dict):
            continue
        for name in names:
            if name in nested:
                return nested[name]
    return None


def _string(value: object | None, *, limit: int = 256) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:limit] if text else None


def _shape(body: dict[str, Any], inner: dict[str, Any] | None = None) -> tuple[str, ...]:
    def safe_name(value: object) -> str:
        name = str(value)
        if name and len(name) <= 64 and all(char.isalnum() or char in "_.-" for char in name):
            return name
        return f"hash-{hashlib.sha256(name.encode()).hexdigest()[:12]}"

    fields = [f"top.{safe_name(key)}:{type(value).__name__}" for key, value in sorted(body.items())]
    if inner is not None:
        fields.extend(
            f"inner.{safe_name(key)}:{type(value).__name__}" for key, value in sorted(inner.items())
        )
    return tuple(fields)


def _shape_hash(fields: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(fields).encode()).hexdigest()[:16]


def _decode_state_message(message: str) -> dict[str, Any]:
    if len(message) > MAX_CALLBACK_BYTES * 2:
        raise VolcCallbackSchemaError("state callback message is too large")
    try:
        decoded = base64.b64decode(message, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise VolcCallbackSchemaError("state callback message is not valid base64") from exc
    if len(decoded) > MAX_CALLBACK_BYTES or len(decoded) < 8:
        raise VolcCallbackSchemaError("state callback binary envelope has invalid size")
    if decoded[:4] != _STATE_MAGIC:
        raise VolcCallbackSchemaError("state callback binary envelope has invalid magic")
    declared_length = int.from_bytes(decoded[4:8], byteorder="big", signed=False)
    payload = decoded[8:]
    if declared_length != len(payload):
        raise VolcCallbackSchemaError("state callback binary envelope length mismatch")
    try:
        inner = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VolcCallbackSchemaError("state callback binary payload is not JSON") from exc
    if not isinstance(inner, dict):
        raise VolcCallbackSchemaError("state callback binary payload must be an object")
    return inner


def parse_state_callback(body: dict[str, Any]) -> StateCallbackEvent:
    signature = _string(callback_value(body, "signature", "Signature"), limit=512)
    message = body.get("message")
    binary = body.get("binary")
    if isinstance(message, str) and binary is True:
        inner = _decode_state_message(message)
        fields = _shape(body, inner)
        stage = inner.get("Stage")
        stage = stage if isinstance(stage, dict) else {}
        stage_code = stage.get("Code")
        stage_description = _string(stage.get("Description"), limit=64)
        if stage_description is None and isinstance(stage_code, int):
            stage_description = _STATE_STAGE_NAMES.get(stage_code)
        error_info = inner.get("ErrorInfo")
        error_info = error_info if isinstance(error_info, dict) else {}
        return StateCallbackEvent(
            signature=signature,
            app_id=_string(inner.get("AppId") or inner.get("AppID"), limit=128),
            room_id=_string(inner.get("RoomId") or inner.get("RoomID"), limit=256),
            task_id=_string(inner.get("TaskId") or inner.get("TaskID"), limit=256),
            run_stage=stage_description,
            error_code=_string(error_info.get("Code"), limit=128),
            error_message=error_info.get("Reason"),
            event_type=(1 if stage_code == 0 else 0),
            provider_trace_id=(
                inner.get("TraceId")
                or inner.get("TraceID")
                or inner.get("RequestId")
                or inner.get("RequestID")
            ),
            event_time=inner.get("EventTime"),
            round_id=inner.get("RoundID"),
            schema_hash=_shape_hash(fields),
            schema_fields=fields,
            schema_kind="conversation_state_binary_v1",
        )

    fields = _shape(body)
    error_info = callback_value(body, "ErrorInfo", "error_info")
    error_info = error_info if isinstance(error_info, dict) else {}
    raw_error_code = callback_value(body, "ErrorCode", "Errorcode", "error_code")
    nested_error_code = callback_value(error_info, "Code", "ErrorCode", "Errorcode", "error_code")
    if raw_error_code in (
        None,
        "",
        0,
        "0",
        "OK",
        "Success",
        "success",
    ) and nested_error_code not in (None, "", 0, "0", "OK", "Success", "success"):
        raw_error_code = nested_error_code
    return StateCallbackEvent(
        signature=signature,
        app_id=_string(
            callback_value(body, "AppId", "AppID", "app_id", "appId", "appid"), limit=128
        ),
        room_id=_string(
            callback_value(body, "RoomId", "RoomID", "room_id", "roomId", "roomid"),
            limit=256,
        ),
        task_id=_string(
            callback_value(body, "TaskId", "TaskID", "task_id", "taskId", "taskid"),
            limit=256,
        ),
        run_stage=_string(
            callback_value(
                body,
                "RunStage",
                "run_stage",
                "Status",
                "status",
                "ConversationStatus",
                "conversation_status",
            ),
            limit=64,
        ),
        error_code=_string(raw_error_code, limit=128),
        error_message=(
            callback_value(body, "ErrorMessage", "error_message")
            or callback_value(error_info, "Reason", "reason", "Message")
        ),
        event_type=callback_value(body, "EventType", "event_type"),
        provider_trace_id=callback_value(
            body, "TraceId", "TraceID", "trace_id", "RequestId", "RequestID"
        ),
        event_time=callback_value(body, "EventTime", "event_time"),
        round_id=callback_value(body, "RoundID", "RoundId", "round_id"),
        schema_hash=_shape_hash(fields),
        schema_fields=fields,
        schema_kind="legacy_json",
    )


def parse_function_callback(body: dict[str, Any]) -> FunctionCallbackEvent:
    fields = _shape(body)
    event_type = _string(callback_value(body, "Type", "type"), limit=64)
    message = callback_value(body, "Message", "message")
    tool_calls: object | None = callback_value(body, "tool_calls", "ToolCalls")
    notification: dict[str, Any] | None = None
    if isinstance(message, str) and event_type in {"tool_calls", "information"}:
        if len(message) > MAX_CALLBACK_BYTES:
            raise VolcCallbackSchemaError("function callback message is too large")
        try:
            decoded = json.loads(message)
        except json.JSONDecodeError as exc:
            raise VolcCallbackSchemaError("function callback message is not JSON") from exc
        if event_type == "tool_calls":
            tool_calls = decoded
        elif isinstance(decoded, dict):
            notification = decoded
        else:
            raise VolcCallbackSchemaError("function information message must be an object")
    return FunctionCallbackEvent(
        signature=_string(callback_value(body, "signature", "Signature"), limit=512),
        app_id=_string(callback_value(body, "AppId", "AppID", "app_id", "appId"), limit=128),
        room_id=_string(callback_value(body, "RoomId", "RoomID", "room_id", "roomId"), limit=256),
        function_task_id=_string(
            callback_value(body, "TaskId", "TaskID", "task_id", "taskId"), limit=256
        ),
        event_type=event_type,
        tool_calls=tool_calls,
        notification=notification,
        schema_hash=_shape_hash(fields),
        schema_fields=fields,
    )
