"""Webhook 地址校验单元测试 —— SSRF 防线的判据表。"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.webhook import (
    WebhookUrlError,
    assert_public_target,
    check_webhook_url,
)


def _resolver(*ips: str):
    async def resolve(host: str, port: int | None) -> list[Any]:
        return [(2, 1, 6, "", (ip, port or 443)) for ip in ips]

    return resolve


@pytest.mark.parametrize(
    "url",
    [
        "https://hooks.example.com/grok",
        "http://example.com:8080/hook?token=abc",
        "https://93.184.216.34/hook",
    ],
)
def test_accepts_public_targets(url: str) -> None:
    assert check_webhook_url(url).hostname is not None


@pytest.mark.parametrize(
    "url",
    [
        # 协议
        "file:///etc/passwd",
        "ftp://example.com/hook",
        "gopher://example.com/",
        "not-a-url",
        "",
        "http://",  # 有协议无主机名
        "https:///hook",
        # loopback / 内网域名
        "http://localhost/hook",
        "http://127.0.0.1:9000/hook",
        "http://[::1]/hook",
        "http://svc.internal/hook",
        "http://printer.local/hook",
        # 私网与保留段
        "http://10.0.0.1/hook",
        "http://172.16.0.1/hook",
        "http://192.168.1.1/hook",
        "http://169.254.169.254/latest/meta-data/",  # 云元数据
        "http://0.0.0.0/hook",
        # 内嵌凭证
        "https://user:pass@example.com/hook",
    ],
)
def test_rejects_unsafe_targets(url: str) -> None:
    with pytest.raises(WebhookUrlError):
        check_webhook_url(url)


async def test_assert_public_target_allows_public_dns_result() -> None:
    target = await assert_public_target(
        "https://hooks.example.com/grok", resolver=_resolver("93.184.216.34")
    )
    assert target == "https://hooks.example.com/grok"


async def test_assert_public_target_blocks_private_dns_result() -> None:
    """域名解析到内网 —— 字面量检查挡不住，必须靠 DNS 层拦。"""
    with pytest.raises(WebhookUrlError, match="非公网地址"):
        await assert_public_target(
            "https://evil.example.com/hook", resolver=_resolver("10.1.2.3")
        )


async def test_assert_public_target_blocks_mixed_dns_result() -> None:
    """多条 A 记录里只要有一条指向内网就拒绝，防 DNS rebinding。"""
    with pytest.raises(WebhookUrlError):
        await assert_public_target(
            "https://evil.example.com/hook",
            resolver=_resolver("93.184.216.34", "127.0.0.1"),
        )


async def test_assert_public_target_rejects_unresolvable_host() -> None:
    async def failing(host: str, port: int | None) -> list[Any]:
        raise OSError("Name or service not known")

    with pytest.raises(WebhookUrlError, match="无法解析"):
        await assert_public_target("https://nope.example.com/hook", resolver=failing)


async def test_assert_public_target_rejects_empty_dns_result() -> None:
    async def empty(host: str, port: int | None) -> list[Any]:
        return []

    with pytest.raises(WebhookUrlError, match="未解析出任何 IP"):
        await assert_public_target("https://nope.example.com/hook", resolver=empty)


async def test_assert_public_target_skips_dns_for_literal_ip() -> None:
    """字面量 IP 已在静态校验判过，不该再触发解析。"""

    async def exploding(host: str, port: int | None) -> list[Any]:
        raise AssertionError("字面量 IP 不应触发 DNS 解析")

    target = await assert_public_target("https://93.184.216.34/hook", resolver=exploding)
    assert target.endswith("/hook")
