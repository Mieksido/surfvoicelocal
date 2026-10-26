"""通用 HTTP 引擎：任何外部服务都能当引擎用，只要遵守这一条契约。

    POST {url}/say
    {"text": "...", "voice": {"id": "...", ...}, "lang": "zh",
     "instruct": "..." | null, "rate": 1.0, "pitch": 0.0}
    ← 200 + wav 字节（16bit PCM，任意采样率；以响应头 X-Sample-Rate 或 wav 头为准）

为什么要有它：
voxd 里 piper / cosy / gsv / cloud 都是"内置引擎"，加一个新的要改代码。
但大多数新需求（ElevenLabs、Azure、自研模型、别人的服务）只是"给我一段文本，
还我一段音频"。把它们统一成这个 HTTP 契约后，**加引擎 = 在 config.toml 里加一段配置**，
不用碰 voxd 的代码。

配置示例（config.toml）：

    [engines.mycloud]
    kind = "http"
    url = "http://127.0.0.1:9999"
    clone = true          # 支持参考音频克隆
    instruct = false      # 支持语气指令
    langs = ["zh", "en"]
    max_chars = 500
    sr = 24000

能力声明（clone / instruct / langs / max_chars / sr）必须写对，路由靠它选引擎。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import wave

import numpy as np

from .base import Caps, EngineError


class HttpEngine:
    def __init__(
        self,
        base_url: str,
        name: str = "http",
        caps: Caps | None = None,
        timeout: float = 120.0,
        headers: dict | None = None,
    ):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.extra_headers = dict(headers or {})
        self.caps = caps or Caps()

    # ── 生命周期 ─────────────────────────────────────────────────────
    def load(self) -> None:
        """可选：约定 GET {url}/health 返回 200 就算在线；没有也不强求。"""
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as r:
                r.read(1)
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 = 服务在，但没实现 /health，可以接受
                raise EngineError(f"{self.name} /health 异常：{e}") from e
        except Exception:  # noqa: BLE001
            # 连不上就让路由去试下一个引擎，而不是把整个请求打挂
            raise EngineError(f"连不上 {self.name}（{self.base_url}）") from None

    def unload(self) -> None:
        pass

    def close(self) -> None:
        pass

    # ── 合成 ─────────────────────────────────────────────────────────
    def render(self, text: str, voice, lang: str = "zh", instruct=None, **kw):
        payload = {
            "text": text,
            "lang": lang or "zh",
            "voice": {
                "id": getattr(voice, "id", ""),
                "ref_audio": getattr(voice, "ref_audio", None),
                "ref_text": getattr(voice, "ref_text", None),
                "preset": getattr(voice, "preset", None),
                "design": getattr(voice, "design", None),
                "speed": getattr(voice, "speed", 1.0),
            },
            "instruct": instruct,
            "rate": kw.get("rate") or 1.0,
            "pitch": kw.get("pitch") or 0.0,
        }
        headers = {"content-type": "application/json", **self.extra_headers}
        req = urllib.request.Request(
            f"{self.base_url}/say",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                hdr_sr = r.headers.get("X-Sample-Rate")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read()[:300].decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            raise EngineError(f"{self.name} 合成失败 {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"{self.name} 合成失败: {e}") from e

        sr, pcm = _decode_wav(raw, hdr_sr)
        y = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        yield y


def _decode_wav(raw: bytes, header_sr: str | None) -> tuple[int, bytes]:
    """接受标准 wav；也接受裸 PCM（此时必须给 X-Sample-Rate）。"""
    if raw[:4] == b"RIFF":
        with wave.open(io.BytesIO(raw)) as w:
            ch = w.getnchannels()
            pcm = w.readframes(w.getnframes())
        if ch > 1:
            pcm = _downmix(pcm, ch)
        return w.getframerate(), pcm
    if header_sr:
        return int(header_sr), raw
    raise EngineError("返回的既不是 wav，也没给 X-Sample-Rate，无法解码")


def _downmix(pcm: bytes, channels: int) -> bytes:
    a = np.frombuffer(pcm, dtype="<i2").reshape(-1, channels).astype(np.int32)
    return (a.mean(axis=1)).astype("<i2").tobytes()
