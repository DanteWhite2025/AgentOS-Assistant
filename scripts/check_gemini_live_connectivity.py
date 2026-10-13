"""Gemini Live（原生音频）连通性与凭证自检脚本。

与 check_volc_connectivity.py / check_openai_realtime_connectivity.py 同一套路：
在写任何业务代码之前，先把这条链路能不能跑通问清楚。

分四步，每步失败都直接给出可执行的修法：
  1. 凭证有效 + 目标模型在 ListModels 里存在；
  2. Live 会话能连通、原生音频真的回流（用长期 API Key 直连）；
  3. 服务端能签发短期凭证（ephemeral token）；
  4. **短期凭证真的能用** —— 这一步才是「数据面直连」的前提。
     第 3 步返回 200 不代表第 4 步能过：实测过 auth_tokens 签发成功、
     但签出来的票在所有传法下都被拒的情况。

运行方式：
    uv run python scripts/check_gemini_live_connectivity.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import ssl
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import certifi
import httpx
import websockets
from dotenv import load_dotenv

root_dir = Path(__file__).resolve().parent.parent
load_dotenv(root_dir / ".env")
sys.path.insert(0, str(root_dir))

WS_HOST = "wss://generativelanguage.googleapis.com"
# 长期 API Key 走标准 BidiGenerateContent。
BIDI_PATH = "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
# 短期票走 v1beta 的 **Constrained** 变体 —— 这是最容易踩的坑：把短期票传给
# 上面那个标准端点，会被拒成 "unregistered callers"，看起来像凭证没签对，
# 其实是端点选错了。端点名取自官方 demo google-gemini/gemini-live-api-examples。
CONSTRAINED_PATH = (
    "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContentConstrained"
)
DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
DEFAULT_VOICE = "Leda"


def _audio_generation_config() -> dict:
    return {
        "responseModalities": ["AUDIO"],
        "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": DEFAULT_VOICE}}},
    }


def _rfc3339(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


async def _live_roundtrip(url: str, model: str, headers: dict[str, str] | None = None) -> dict:
    """连一次 Live 会话，发一句话，统计音频回流。抛异常即代表这条路不通。"""

    started = time.time()
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    async with websockets.connect(
        url,
        additional_headers=headers or {},
        max_size=None,
        ssl=ssl_context,
    ) as ws:
        handshake_ms = int((time.time() - started) * 1000)
        await ws.send(
            json.dumps(
                {
                    "setup": {
                        "model": f"models/{model}",
                        "generationConfig": _audio_generation_config(),
                    }
                }
            )
        )
        raw = await asyncio.wait_for(ws.recv(), timeout=20)
        payload = json.loads(raw if isinstance(raw, str) else raw.decode())
        if "setupComplete" not in payload:
            raise RuntimeError("setupComplete was not received")

        started = time.time()
        await ws.send(
            json.dumps(
                {
                    "clientContent": {
                        "turns": [{"role": "user", "parts": [{"text": "你好，请用一句话回答。"}]}],
                        "turnComplete": True,
                    }
                }
            )
        )
        audio_bytes = 0
        first_packet_ms: int | None = None
        mime = ""
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=30)
            message = json.loads(raw if isinstance(raw, str) else raw.decode())
            content = message.get("serverContent") or {}
            for part in (content.get("modelTurn") or {}).get("parts", []):
                inline = part.get("inlineData")
                if inline and str(inline.get("mimeType", "")).startswith("audio/"):
                    if first_packet_ms is None:
                        first_packet_ms = int((time.time() - started) * 1000)
                        mime = str(inline["mimeType"])
                    audio_bytes += len(base64.b64decode(inline.get("data", "")))
            if content.get("turnComplete") or content.get("generationComplete"):
                break
    return {
        "handshake_ms": handshake_ms,
        "first_packet_ms": first_packet_ms,
        "audio_bytes": audio_bytes,
        "mime": mime,
    }


async def check() -> bool:
    print("=" * 70)
    print("🚀 Gemini Live 原生音频 —— 连通性与凭证体检")
    print("=" * 70)

    api_key = os.getenv("AGENT_GEMINI_API_KEY", "").strip()
    base_url = os.getenv("AGENT_GEMINI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    model = os.getenv("AGENT_GEMINI_LIVE_MODEL", DEFAULT_MODEL).strip()

    if not api_key:
        print("\n❌ 未检测到 AGENT_GEMINI_API_KEY")
        print("👉 在 .env 中配置：AGENT_GEMINI_API_KEY=...")
        return False

    print("\n  • Gemini API credential: configured")
    print(f"  • Base URL: {base_url}")
    print(f"  • Model:    {model}")

    async with httpx.AsyncClient(timeout=25.0) as client:
        # ---- 1. 凭证与模型可见性 ----
        print("\n[1/4] 校验凭证有效、模型在 ListModels 中存在…")
        started = time.time()
        response = await client.get(
            f"{base_url}/v1beta/models", headers={"x-goog-api-key": api_key}
        )
        if response.status_code != 200:
            print(f"  ❌ HTTP {response.status_code}")
            print("  👉 凭证无效，或该项目未开通 Generative Language API")
            return False
        names = {str(m.get("name", "")) for m in response.json().get("models", [])}
        if f"models/{model}" not in names:
            print(f"  ❌ 模型 {model} 不在可用列表中")
            print("  👉 请确认项目已开通 Gemini 3.1 Flash Live Preview")
            return False
        print(f"  ✅ 凭证有效、模型存在（{int((time.time() - started) * 1000)}ms）")

        # ---- 2. Live 会话 + 原生音频（长期凭证直连）----
        print("\n[2/4] 用长期凭证连 Live 会话，确认原生音频回流…")
        try:
            stats = await _live_roundtrip(
                f"{WS_HOST}{BIDI_PATH}?key={api_key}", model
            )
        except Exception as exc:
            # WebSocket 异常可能包含带 Key 的完整 URI，只输出异常类型。
            print(f"  ❌ {type(exc).__name__}")
            return False
        if not stats["audio_bytes"]:
            print("  ❌ 会话建立了，但没有收到任何音频数据")
            return False
        print(
            f"  ✅ 握手 {stats['handshake_ms']}ms · 首个音频包 {stats['first_packet_ms']}ms · "
            f"{stats['audio_bytes']} 字节 · {stats['mime']}"
        )

        # ---- 3. 短期凭证签发 ----
        print("\n[3/4] 签发短期凭证（ephemeral token）…")
        now = datetime.now(UTC)
        response = await client.post(
            f"{base_url}/v1beta/auth_tokens",
            headers={"x-goog-api-key": api_key},
            json={
                "uses": 1,
                "expireTime": _rfc3339(now + timedelta(minutes=30)),
                # newSessionExpireTime 是「这张票还能用来开新会话」的窗口，
                # 官方 demo 给 1 分钟：客户端拿到票应当立刻连。
                "newSessionExpireTime": _rfc3339(now + timedelta(minutes=1)),
                "bidiGenerateContentSetup": {
                    "model": f"models/{model}",
                    "generationConfig": _audio_generation_config(),
                    "systemInstruction": {
                        "parts": [{"text": "必须始终使用简体中文回答。"}]
                    },
                    "tools": [
                            {
                                "functionDeclarations": [
                                    {
                                        "name": "market_tickers",
                                        "description": "查询只读行情",
                                        "parameters": {
                                            "type": "object",
                                            "properties": {
                                                "symbols": {
                                                    "type": "array",
                                                    "items": {"type": "string"},
                                                }
                                            },
                                        },
                                    }
                                ]
                            }
                        ],
                },
            },
        )
        if response.status_code != 200:
            print(f"  ❌ HTTP {response.status_code}")
            print("  👉 这个凭证没有签发短期票的权限")
            return False
        token = str(response.json().get("name") or "")
        if not token:
            print("  ❌ 响应缺少 name 字段")
            return False
        print("  ✅ 单次短期凭证已签发（值未输出）")

    # ---- 4. 短期凭证可用性（决定架构能否成立）----
    print("\n[4/4] 验证短期票真的能连上 Live（客户端直连的前提）…")
    print("      注意：第 3 步 200 不等于这一步能过。")
    try:
        stats = await _live_roundtrip(
            f"{WS_HOST}{CONSTRAINED_PATH}?access_token={token}", model
        )
    except Exception as exc:
        print(f"  ❌ {type(exc).__name__}")
        return _report_ephemeral_failure()
    if not stats["audio_bytes"]:
        print("  ❌ 会话建立了，但没有收到任何音频数据")
        return _report_ephemeral_failure()
    print(
        f"  ✅ 握手 {stats['handshake_ms']}ms · 首个音频包 {stats['first_packet_ms']}ms · "
        f"{stats['audio_bytes']} 字节 · {stats['mime']}"
    )

    print("\n" + "=" * 70)
    print("🎉 全部通过：服务端签票 → 客户端直连 → 原生音频回流")
    print("=" * 70)
    print(
        "\n架构结论：\n"
        "  · 数据面直连成立 —— 音频不经过 Runtime，长期 API Key 不下发到客户端。\n"
        "  · 传输是 WebSocket（BidiGenerateContentConstrained），不是 WebRTC。\n"
        "    Gemini Live 官方没有原生 WebRTC，要 WebRTC 得经 LiveKit/Pipecat 之类的\n"
        "    第三方桥接。「音频不过服务器」这条架构约束不变，只是客户端换一种连法。"
    )
    return True


def _report_ephemeral_failure() -> bool:
    print("\n" + "=" * 70)
    print("⚠️  链路可用，但【短期凭证不可用】—— 数据面直连暂不成立")
    print("=" * 70)
    print(
        "\n现状：\n"
        "  ✅ 长期凭证能连 Live、原生音频正常\n"
        "  ✅ auth_tokens 签发返回 200\n"
        "  ❌ 签出来的票连 Constrained 端点仍被拒\n"
        "\n影响：\n"
        "  客户端直连需要一张能用的短期票。只剩长期 API Key 能连的话，就得把\n"
        "  项目级长期凭证下发到手机上 —— 这条不能做。\n"
        "\n下一步：\n"
        "  1. 确认端点是 v1beta 的 BidiGenerateContentConstrained（标准 BidiGenerateContent\n"
        "     不接受短期票，会误报成凭证问题）。\n"
        "  2. 换一把 AI Studio「Create API key」签发的标准项目 API Key 重试。\n"
        "  3. 仍不通说明该项目未开通 ephemeral token，需在 Google Cloud 侧开通。"
    )
    return False


if __name__ == "__main__":
    raise SystemExit(0 if asyncio.run(check()) else 1)
