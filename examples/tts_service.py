"""示例：一个最小的外部 TTS 服务，用来照着接自己的引擎。

它证明"接一个新语音服务"可以完全不碰 voxd 的代码：

    1. 复制这个文件，把 render() 换成你自己的模型/云 API
    2. 启动它：python examples/tts_service.py --port 9900
    3. 在 config.toml 里加：
           [engines.mydemo]
           kind = "http"
           url = "http://127.0.0.1:9900"
           clone = false
           langs = ["zh"]
           sr = 24000
    4. 重启 voxd，`curl 127.0.0.1:8765/v1/engines` 就能看到 mydemo
    5. 建一个音色 voices/demo/voice.json：{"engine": "mydemo"}

契约就两条：
    GET  /health  → 200（可以不实现，404 也算"服务在"）
    POST /say     → 请求里取 text，返回 wav（或裸 PCM + X-Sample-Rate 头）
"""

from __future__ import annotations

import argparse
import io
import math
import struct
import wave

from fastapi import FastAPI, Request, Response

app = FastAPI(title="example-tts")


def _tone_wav(text: str, sr: int = 24000) -> bytes:
    """占位实现：按字数生成一段"嘀"声，只为验证链路。

    换成你的真实实现时，这里返回真正的语音即可。
    """
    secs = max(0.25, min(3.0, len(text) * 0.08))
    n = int(sr * secs)
    # 三个不同音高，听感上能区分长度
    freq = 220 + (len(text) % 5) * 40
    frames = bytearray()
    for i in range(n):
        # 简单的包络，避免爆音
        env = min(1.0, i / (sr * 0.02), (n - i) / (sr * 0.05))
        v = int(12000 * env * math.sin(2 * math.pi * freq * i / sr))
        frames += struct.pack("<h", v)

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(bytes(frames))
    return buf.getvalue()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/say")
async def say(request: Request):
    payload = await request.json()
    text = str(payload.get("text", ""))
    voice = payload.get("voice") or {}

    # 真实实现里，这里用 payload["text"] / voice / instruct / rate 去合成
    audio = _tone_wav(text)

    return Response(
        content=audio,
        media_type="audio/wav",
        headers={"X-Sample-Rate": "24000", "X-Voice-Id": str(voice.get("id", ""))},
    )


if __name__ == "__main__":
    import uvicorn

    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9900)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
