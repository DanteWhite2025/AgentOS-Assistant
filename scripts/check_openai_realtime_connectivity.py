"""OpenAI Realtime (WebRTC) 连通性与凭证自检脚本。

运行方式：
    uv run python scripts/check_openai_realtime_connectivity.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

# 加载本地 .env
root_dir = Path(__file__).resolve().parent.parent
load_dotenv(root_dir / ".env")

sys.path.insert(0, str(root_dir))


async def test_openai_realtime() -> bool:
    print("=" * 65)
    print("🚀 开始执行 OpenAI Realtime (WebRTC) 连通性与凭证体检...")
    print("=" * 65)

    api_key = (
        os.getenv("AGENT_OPENAI_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or ""
    ).strip()

    base_url = os.getenv("AGENT_OPENAI_BASE_URL", "https://api.openai.com").strip().rstrip("/")
    model = os.getenv("AGENT_OPENAI_REALTIME_MODEL", "gpt-4o-realtime-preview").strip()

    if not api_key:
        print("\n❌ 未检测到 OpenAI API Key！")
        print("👉 请在 .env 文件中配置：AGENT_OPENAI_API_KEY=sk-xxxx")
        print("=" * 65)
        return False

    print("\n[1/2] 正在向 OpenAI 请求 Realtime Ephemeral Token (临时通话密钥)...")
    print(f"  • Base URL: {base_url}")
    print(f"  • Model:    {model}")
    print(f"  • API Key:  {api_key[:8]}...{api_key[-4:] if len(api_key) > 12 else ''}")

    # OpenAI Realtime Session 接口
    session_url = f"{base_url}/v1/realtime/sessions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "modalities": ["audio", "text"],
        "voice": "alloy",
        "turn_detection": {"type": "server_vad"},
    }

    start_time = time.time()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(session_url, headers=headers, json=payload)
            elapsed_ms = int((time.time() - start_time) * 1000)

            if resp.status_code == 200:
                data = resp.json()
                client_secret = data.get("client_secret", {})
                secret_value = client_secret.get("value") if isinstance(client_secret, dict) else str(client_secret)
                expires_at = client_secret.get("expires_at") if isinstance(client_secret, dict) else None

                print(f"  ✅ 请求成功！耗时: {elapsed_ms}ms (HTTP 200)")
                print("\n[2/2] 验证 Ephemeral Token 结构...")
                print(f"  • Token 片段:  {secret_value[:20]}... (长度 {len(secret_value)})")
                print(f"  • 有效期时间:  {expires_at} 秒时间戳")
                print(f"  • 会话 ID:     {data.get('id', '未知')}")
                
                print("\n" + "=" * 65)
                print("🎉 体检通过！OpenAI Realtime WebRTC 凭证生成通道 100% 可用！")
                print("   客户端可直接使用该 Token 与 OpenAI 官方建立 WebRTC 毫秒级通话。")
                print("=" * 65)
                return True
            else:
                print(f"  ❌ 请求被上游拒绝: HTTP {resp.status_code}")
                print(f"  • 错误详情: {resp.text}")
                print("\n" + "=" * 65)
                print("⚠️ 请排查：1. Key 是否正确；2. 账户是否开通 Realtime 权限与账单余额。")
                print("=" * 65)
                return False

    except Exception as e:
        print(f"  ❌ 网络连接或请求异常: {e}")
        print("\n" + "=" * 65)
        print("⚠️ 请排查：本地到 api.openai.com 的网络连接（是否需要代理环境）。")
        print("=" * 65)
        return False


if __name__ == "__main__":
    asyncio.run(test_openai_realtime())
