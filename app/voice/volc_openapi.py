"""火山 RTC OpenAPI 的服务端签名客户端。"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx


class VolcOpenAPIError(RuntimeError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hmac(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode(), hashlib.sha256).digest()


class VolcRtcOpenAPI:
    def __init__(
        self,
        *,
        access_key_id: str,
        secret_access_key: str,
        client: httpx.AsyncClient,
        endpoint: str = "https://rtc.volcengineapi.com",
        region: str = "cn-north-1",
        version: str = "2024-12-01",
    ) -> None:
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client = client
        self._endpoint = endpoint.rstrip("/")
        self._region = region
        self._version = version

    def signed_request(
        self, action: str, payload: dict[str, Any], now: datetime
    ) -> tuple[str, bytes, dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        x_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        short_date = x_date[:8]
        host = httpx.URL(self._endpoint).host
        query = "&".join(
            f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
            for key, value in sorted({"Action": action, "Version": self._version}.items())
        )
        payload_hash = _sha256(body)
        # 与火山官方 @volcengine/openapi Signer 一致：Content-Type 属于不签名头。
        canonical_headers = f"host:{host}\nx-content-sha256:{payload_hash}\nx-date:{x_date}\n"
        signed_headers = "host;x-content-sha256;x-date"
        canonical_request = (
            f"POST\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        scope = f"{short_date}/{self._region}/rtc/request"
        string_to_sign = "\n".join(
            ("HMAC-SHA256", x_date, scope, _sha256(canonical_request.encode()))
        )
        signing_key = _hmac(
            _hmac(_hmac(_hmac(self._secret_access_key.encode(), short_date), self._region), "rtc"),
            "request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return (
            f"{self._endpoint}/?{query}",
            body,
            {
                "Content-Type": "application/json",
                "Host": host,
                "X-Content-Sha256": payload_hash,
                "X-Date": x_date,
                "Authorization": authorization,
            },
        )

    def signed_query_request(
        self,
        action: str,
        params: dict[str, str],
        now: datetime,
        *,
        version: str,
    ) -> tuple[str, dict[str, str]]:
        """Sign a GET OpenAPI request (used for authoritative RTC room presence)."""

        x_date = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
        short_date = x_date[:8]
        host = httpx.URL(self._endpoint).host
        query_params = {"Action": action, "Version": version, **params}
        query = "&".join(
            f"{quote(key, safe='-_.~')}={quote(value, safe='-_.~')}"
            for key, value in sorted(query_params.items())
        )
        payload_hash = _sha256(b"")
        canonical_headers = f"host:{host}\nx-content-sha256:{payload_hash}\nx-date:{x_date}\n"
        signed_headers = "host;x-content-sha256;x-date"
        canonical_request = (
            f"GET\n/\n{query}\n{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )
        scope = f"{short_date}/{self._region}/rtc/request"
        string_to_sign = "\n".join(
            ("HMAC-SHA256", x_date, scope, _sha256(canonical_request.encode()))
        )
        signing_key = _hmac(
            _hmac(_hmac(_hmac(self._secret_access_key.encode(), short_date), self._region), "rtc"),
            "request",
        )
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            f"HMAC-SHA256 Credential={self._access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return (
            f"{self._endpoint}/?{query}",
            {
                "Host": host,
                "X-Content-Sha256": payload_hash,
                "X-Date": x_date,
                "Authorization": authorization,
            },
        )

    @staticmethod
    def _validate_response(data: object) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise VolcOpenAPIError("INVALID_RESPONSE: Volc RTC returned a non-object response")
        metadata = data.get("ResponseMetadata")
        error = metadata.get("Error") if isinstance(metadata, dict) else None
        if error:
            code = error.get("Code", "UNKNOWN")
            message = error.get("Message", "Volc RTC OpenAPI rejected the request")
            raise VolcOpenAPIError(f"{code}: {message}")
        return data

    async def call(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        url, body, headers = self.signed_request(action, payload, datetime.now(UTC))
        response = await self._client.post(url, content=body, headers=headers)
        response.raise_for_status()
        return self._validate_response(response.json())

    async def query(
        self, action: str, params: dict[str, str], *, version: str = "2023-11-01"
    ) -> dict[str, Any]:
        url, headers = self.signed_query_request(
            action, params, datetime.now(UTC), version=version
        )
        response = await self._client.get(url, headers=headers)
        response.raise_for_status()
        return self._validate_response(response.json())
