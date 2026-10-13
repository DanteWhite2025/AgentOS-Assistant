"""火山引擎 (Volcengine) 连通性与凭证自检脚本。

运行方式：
    python scripts/check_volc_connectivity.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import load_dotenv

# 加载本地 .env
root_dir = Path(__file__).resolve().parent.parent
load_dotenv(root_dir / ".env")

sys.path.insert(0, str(root_dir))

from app.voice.volc_rtc_token import generate_rtc_token


async def test_huochan_tts() -> bool:
    print("\n[1/2] 正在测试火山语音合成 (TTS 2.0)...")
    api_key = os.getenv("AGENT_HUOCHAN_API_KEY", "").strip()
    tts_url = os.getenv(
        "AGENT_HUOCHAN_TTS_URL",
        "https://openspeech.bytedance.com/api/v3/tts/unidirectional/sse",
    ).strip()
    voice_type = os.getenv("AGENT_HUOCHAN_VOICE_TYPE", "zh_female_vv_uranus_bigtts").strip()
    resource_id = os.getenv("AGENT_HUOCHAN_RESOURCE_ID", "seed-tts-2.0").strip()

    if not api_key:
        print("  ⚠️ AGENT_HUOCHAN_API_KEY 未配置，跳过 TTS 连通性测试。")
        return False

    headers = {
        "Authorization": f"Bearer;{api_key}",
        "Content-Type": "application/json",
        "Resource-Id": resource_id,
    }
    payload = {
        "user": {"uid": "test_user_001"},
        "audio": {
            "voice_type": voice_type,
            "encoding": "mp3",
            "speed_ratio": 1.0,
        },
        "request": {
            "reqid": "test-ping-req",
            "text": "火山语音服务连接正常，欢迎使用。",
            "operation": "query",
        },
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(tts_url, headers=headers, json=payload)
            if resp.status_code == 200 and len(resp.content) > 0:
                print(f"  ✅ 火山 TTS 连接成功！HTTP 200，收到响应数据 ({len(resp.content)} bytes)")
                return True
            else:
                print(f"  ❌ 火山 TTS 请求失败: HTTP {resp.status_code}, body: {resp.text[:200]}")
                return False
    except Exception as e:
        print(f"  ❌ 火山 TTS 请求异常: {e}")
        return False


def test_volc_rtc_token_generation() -> bool:
    print("\n[2/2] 正在测试火山 RTC 进房 Token 本地算法...")
    app_id = os.getenv("AGENT_RTC_APP_ID", "123456789012345678901234").strip()
    app_key = os.getenv("AGENT_RTC_APP_KEY", "test-key-32-chars-long-abcdef").strip()

    try:
        now = datetime.now(UTC)
        token = generate_rtc_token(
            app_id=app_id,
            app_key=app_key,
            room_id="test_room_001",
            user_id="test_user_001",
            issued_at=now,
            expires_at=datetime.fromtimestamp(now.timestamp() + 3600, UTC),
        )
        print(f"  ✅ 火山 RTC Token 本地生成成功！\n     AppID: {app_id[:6]}***\n     Token: {token[:25]}... (长度 {len(token)})")
        return True
    except Exception as e:
        print(f"  ❌ 火山 RTC Token 生成异常: {e}")
        return False


async def main():
    print("=" * 60)
    print("🚀 开始执行火山引擎连通性与配置体检...")
    print("=" * 60)

    tts_ok = await test_huochan_tts()
    rtc_token_ok = test_volc_rtc_token_generation()

    print("\n" + "=" * 60)
    print("📊 体检结果汇总：")
    print(f"  - 火山 TTS (语音合成): {'✅ 正常可用' if tts_ok else '❌ 异常/未配置'}")
    print(f"  - 火山 RTC Token 算法: {'✅ 正常可用' if rtc_token_ok else '❌ 异常'}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
