"""Webhook 目标地址校验 —— 防 SSRF。

`/grok/push` 允许调用方指定推送目标，等于把「让服务器发起请求」的能力交给外部；
不校验就能被用来打云元数据端点（169.254.169.254）或内网服务。两层校验：

1. `check_webhook_url` —— 纯函数：协议 / 凭证 / 字面量 IP，无网络 IO，可离线单测。
2. `assert_public_target` —— 解析 DNS 并逐个校验解析结果，挡住「域名指向内网」这类
   绕过字面量检查的用法。`resolver` 可注入，保证单测不依赖真实 DNS。

判据沿用 `app/config.py` 对火山 callback URL 的同一套标准（非 loopback + is_global）。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Final
from urllib.parse import ParseResult, urlparse

import httpx

logger = logging.getLogger(__name__)

_ALLOWED_SCHEMES: Final = frozenset({"http", "https"})
_BLOCKED_HOSTS: Final = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})
_BLOCKED_SUFFIXES: Final = (".localhost", ".local", ".internal")

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int | None], Awaitable[Sequence[Any]]]


class WebhookUrlError(ValueError):
    """Webhook 目标地址不可用：协议不对、带凭证，或指向非公网地址。"""


def _parse_ip(host: str) -> IpAddress | None:
    """把主机名当字面量 IP 解析；不是 IP 则返回 None（交由 DNS 层处理）。"""
    try:
        return ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return None


def check_webhook_url(raw_url: str) -> ParseResult:
    """校验 URL 的静态部分。通过则返回解析结果，否则抛 WebhookUrlError。"""
    parsed = urlparse(raw_url.strip())

    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        raise WebhookUrlError("webhook_url 仅支持 http / https")

    host = (parsed.hostname or "").lower()
    if not host:
        raise WebhookUrlError("webhook_url 缺少合法主机名")

    if parsed.username is not None or parsed.password is not None:
        raise WebhookUrlError("webhook_url 不得内嵌用户名或密码")

    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise WebhookUrlError(f"webhook_url 不得指向 loopback 或内网域名: {host}")

    address = _parse_ip(host)
    if address is not None and not address.is_global:
        raise WebhookUrlError(f"webhook_url 必须指向公网地址，拒绝: {host}")

    return parsed


async def _default_resolver(host: str, port: int | None) -> Sequence[Any]:
    loop = asyncio.get_running_loop()
    return await loop.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)


async def assert_public_target(raw_url: str, resolver: Resolver | None = None) -> str:
    """静态校验 + DNS 校验。通过则返回规范化后的 URL。"""
    parsed = check_webhook_url(raw_url)
    host = (parsed.hostname or "").lower()
    target = raw_url.strip()

    if _parse_ip(host) is not None:
        return target  # 字面量 IP 已在静态校验里判过，无需再解析

    resolve = resolver or _default_resolver
    try:
        infos = await resolve(host, parsed.port)
    except OSError as exc:
        raise WebhookUrlError(f"webhook_url 主机名无法解析: {host}") from exc

    addresses = [
        address
        for info in infos
        if (address := _parse_ip(str(info[4][0]))) is not None
    ]
    if not addresses:
        raise WebhookUrlError(f"webhook_url 主机名未解析出任何 IP: {host}")

    for address in addresses:
        if not address.is_global:
            raise WebhookUrlError(
                f"webhook_url 主机名 {host} 解析到非公网地址 {address}，已拒绝"
            )
    return target


async def post_webhook(
    client: httpx.AsyncClient,
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float = 10.0,
    resolver: Resolver | None = None,
) -> None:
    """校验目标地址后推送。非 2xx 或网络异常一律抛出，由调用方决定如何上报。"""
    target = await assert_public_target(url, resolver=resolver)
    response = await client.post(target, json=payload, timeout=timeout)
    response.raise_for_status()
