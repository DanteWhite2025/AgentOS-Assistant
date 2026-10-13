"""量测 02 方案流水线的首包时延（打真实火山 TTS）。

两个数字：

* **单句 TTS 首包** —— 一次 SSE 请求到第一个音频分片，目标 < 300ms；
* **流水线首包** —— 模拟 LLM 逐字吐字，到第一句音频就绪，目标 < 1.3s。

用法（需要 .env 里有 AGENT_HUOCHAN_API_KEY）：

    .venv/bin/python scripts/measure_voice_pipeline_latency.py

产物落在 demo_voices/pipeline_latency.mp3，可以直接试听音色。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from app.config import Settings
from app.voice.pipeline_provider import AudioChunk, VoicePipeline
from app.voice.volc_tts import VolcengineTTSClient, VolcTTSConfig

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "demo_voices" / "pipeline_latency.mp3"

ANSWER = (
    "你好呀！我刚帮你看了下，比特币现在报 68000 美元，24 小时涨了 3.5%。"
    "你的 USDT 余额还够开一笔小仓，要不要我先起一张做多草案给你核对？"
)
# 模拟大模型吐字节奏：真链路实测约每秒 25~40 个 token。
TOKEN_INTERVAL_SECONDS = 0.03


def _config(settings: Settings) -> VolcTTSConfig:
    return VolcTTSConfig(
        api_key=settings.huochan_api_key,
        url=settings.huochan_tts_url,
        resource_id=settings.huochan_resource_id,
        voice=settings.huochan_voice_type,
        speed_ratio=settings.huochan_speed_ratio,
    )


async def _measure_single_sentence(client: VolcengineTTSClient) -> float:
    started = time.perf_counter()
    async for _chunk in client.stream_synthesize("你好呀！"):
        return time.perf_counter() - started
    raise RuntimeError("火山没有返回任何音频分片")


async def _warm_up(client: VolcengineTTSClient) -> None:
    """先打一次把 TLS 握手和连接建立的成本排除掉。

    线上是常驻进程 + 连接池复用，冷启动的那一次不代表稳态时延。
    """

    async for _chunk in client.stream_synthesize("预热"):
        break


async def _typed_tokens() -> AsyncIterator[str]:
    """逐字吐出答案，模拟真实的 LLM 流式节奏。"""

    for char in ANSWER:
        await asyncio.sleep(TOKEN_INTERVAL_SECONDS)
        yield char


async def _measure_pipeline(pipeline: VoicePipeline) -> tuple[float, int, bytes]:
    started = time.perf_counter()
    first_chunk_at: float | None = None
    audio = bytearray()
    # is_first 标记的是「整轮的第一个分片」（前端据此退出思考动画），不是
    # 「每句的第一个分片」—— 数句子要按 index 去重。
    spoken: set[int] = set()

    turn_id = pipeline.begin_turn()
    async for event in pipeline.stream_turn(_typed_tokens(), turn_id=turn_id):
        if not isinstance(event, AudioChunk):
            print(f"  ⚠️  第 {event.index} 句降级为文本（{event.reason}）：{event.text}")
            continue
        if first_chunk_at is None:
            first_chunk_at = time.perf_counter() - started
        spoken.add(event.index)
        audio.extend(event.audio)

    if first_chunk_at is None:
        raise RuntimeError("整轮没有产出任何音频")
    return first_chunk_at, len(spoken), bytes(audio)


async def main() -> None:
    # 量测只用到 TTS 那几个配置项，跟主链路是哪家无关。显式降到 fake 模式是为了
    # 绕开 real 模式对 RTC/Core 整套凭证的校验 —— 本地 .env 里主链路可能是别家。
    settings = Settings(provider_mode="fake", env="local")
    if not settings.huochan_api_key.strip():
        raise SystemExit("缺少 AGENT_HUOCHAN_API_KEY，无法量测")

    print(f"音色: {settings.huochan_voice_type}  语速: {settings.huochan_speed_ratio}")

    async with httpx.AsyncClient(timeout=15.0) as http:
        client = VolcengineTTSClient(http, _config(settings))

        cold = await _measure_single_sentence(client)
        print(f"单句 TTS 首包（冷启动，含 TLS 握手）: {cold * 1000:.0f} ms")

        await _warm_up(client)
        samples = [await _measure_single_sentence(client) for _ in range(3)]
        best = min(samples) * 1000
        worst = max(samples) * 1000
        print(
            f"单句 TTS 首包（连接复用，3 次）: "
            f"{best:.0f} ~ {worst:.0f} ms  (目标 < 300 ms)"
        )

        pipeline = VoicePipeline(client)
        first, sentences, audio = await _measure_pipeline(pipeline)
        print(f"流水线首包:   {first * 1000:.0f} ms  (目标 < 1300 ms)")
        print(f"整轮: {sentences} 句 / {len(audio)} 字节音频")

    OUTPUT.parent.mkdir(exist_ok=True)
    OUTPUT.write_bytes(audio)
    print(f"音频已落盘: {OUTPUT.relative_to(ROOT)}")


if __name__ == "__main__":
    asyncio.run(main())
