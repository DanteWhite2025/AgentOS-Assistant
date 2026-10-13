"""流式语音识别（WSS /v1/agent/audio/stream）联调脚本。

连本地（或任意部署）Runtime，把一个 WAV 文件按 100ms 一帧切片、当成实时录音
发送，打字机式打印 ``transcript.delta`` / ``transcript.done``。没给音频文件时
生成一段静音占位，只用来验证握手 + 协议往返（真识别当然是空字符串）。

用法：
    .venv/bin/python scripts/demo_stream_asr.py --token test-user:demo
    .venv/bin/python scripts/demo_stream_asr.py --token test-user:demo --audio test.wav
    .venv/bin/python scripts/demo_stream_asr.py --url ws://127.0.0.1:8811/v1/agent/audio/stream \\
        --token test-user:demo --audio test.wav

WAV 必须是 16-bit PCM（`.wav` 容器头会被去掉，只发裸 PCM——真实设备录音同理，
音频格式协商全靠 ``audio.start`` 里的 ``format``/``sample_rate``/``channels``，
不依赖文件容器）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import wave
from contextlib import suppress

import websockets

_FRAME_MS = 100
_SAMPLE_RATE = 16000
_SILENCE_SECONDS = 1.0


def _load_pcm16_mono(path: str) -> tuple[bytes, int]:
    with wave.open(path, "rb") as wav_file:
        if wav_file.getsampwidth() != 2:
            raise SystemExit(f"{path} 不是 16-bit PCM WAV")
        sample_rate = wav_file.getframerate()
        frames = wav_file.readframes(wav_file.getnframes())
        if wav_file.getnchannels() == 2:
            # 简单降混：只取左声道，够联调用。
            frames = frames[0::4] + frames[1::4]
        return frames, sample_rate


def _silence(sample_rate: int, seconds: float) -> bytes:
    return b"\x00\x00" * int(sample_rate * seconds)


def _chunks(pcm: bytes, sample_rate: int, frame_ms: int) -> list[bytes]:
    bytes_per_frame = int(sample_rate * frame_ms / 1000) * 2  # 16-bit mono
    return [pcm[i : i + bytes_per_frame] for i in range(0, len(pcm), bytes_per_frame)] or [b""]


async def _run(url: str, token: str, audio_path: str | None) -> None:
    if audio_path:
        pcm, sample_rate = _load_pcm16_mono(audio_path)
    else:
        sample_rate = _SAMPLE_RATE
        pcm = _silence(sample_rate, _SILENCE_SECONDS)
        print("(未提供 --audio，发送一段静音占位，只验证协议往返)", file=sys.stderr)

    full_url = f"{url}?token={token}"
    print(f"连接 {full_url} ...", file=sys.stderr)

    async with websockets.connect(full_url) as ws:
        await ws.send(
            json.dumps(
                {
                    "type": "audio.start",
                    "data": {"format": "pcm", "sample_rate": sample_rate, "channels": 1},
                }
            )
        )

        async def _send_audio() -> None:
            for chunk in _chunks(pcm, sample_rate, _FRAME_MS):
                if chunk:
                    await ws.send(chunk)
                await asyncio.sleep(_FRAME_MS / 1000)
            await ws.send(json.dumps({"type": "audio.end"}))

        sender = asyncio.create_task(_send_audio())
        try:
            while True:
                raw = await ws.recv()
                event = json.loads(raw)
                etype = event.get("type")
                data = event.get("data") or {}
                if etype == "transcript.delta":
                    print(f"\r{data.get('text', '')}", end="", flush=True)
                elif etype == "transcript.done":
                    print(f"\r{data.get('text', '')}")
                    break
                elif etype == "error":
                    print(f"\n[error] {data.get('code')}: {data.get('message')}", file=sys.stderr)
                    break
                else:
                    print(f"\n[未知事件] {event}", file=sys.stderr)
        finally:
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await sender


def main() -> None:
    parser = argparse.ArgumentParser(description="流式语音识别联调脚本")
    parser.add_argument(
        "--url", default="ws://127.0.0.1:8000/v1/agent/audio/stream", help="WSS 端点（默认本地 8000 端口）"
    )
    parser.add_argument("--token", required=True, help="USER_TOKEN 或 rt_token（不带 Bearer 前缀）")
    parser.add_argument("--audio", default=None, help="16-bit PCM WAV 文件路径；不传则发送静音占位")
    args = parser.parse_args()

    asyncio.run(_run(args.url, args.token, args.audio))


if __name__ == "__main__":
    main()
