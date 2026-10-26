"""GPT-SoVITS 引擎：接本地微调出来的 age12。

GPT-SoVITS 官方自带的 `api_v2.py` 就是个 HTTP 服务（默认 9880），
它已经处理好了"参考音频 + 提示文本 → 任意文本合成"的整套流程。
所以我们不重写推理，只写一个薄客户端：

    POST {base}/tts  {text, text_lang, ref_audio_path, prompt_text, prompt_lang, 采样参数}
    ← wav 字节

音色配置在 voices/<id>/voice.json 里：
    engine      = "gsv"
    ref         = 参考音频路径（3~10 秒，越像目标越好）
    ref_text    = 参考音频对应的文字（必须逐字一致）
    temperature / top_k / top_p   （不填用引擎默认：1.3 / 20 / 0.95）
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import wave

import numpy as np

from .base import Caps, EngineError


class GsvEngine:
    def __init__(self, base_url: str, name: str = "gsv", timeout: float = 300.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # 微调后的模型音色固定，靠换参考音频继承韵律；没有 instruct 概念
        self.caps = Caps(
            clone=True,
            instruct=False,
            langs=("zh", "en", "ja"),
            max_chars=200,
            sr=24000,  # 引擎内部统一重采样到 24k 再交出去，避免任何一层搞错
            instruct_mode="none",
        )

    # ── 生命周期 ─────────────────────────────────────────────────────
    def load(self) -> None:
        try:
            with urllib.request.urlopen(f"{self.base_url}/docs", timeout=5) as r:
                r.read(1)
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"连不上 gsv 服务（{self.base_url}）：{e}") from e

    def unload(self) -> None:
        pass

    # ── 合成 ─────────────────────────────────────────────────────────
    def _sampling(self, voice) -> dict:
        def pick(key: str, default):
            v = getattr(voice, key, None)
            return default if v is None else v

        # 采样参数 + 种子一起固定：同一个人、同一套采样路径，音色才不会再飘
        out = {
            "temperature": pick("temperature", 1.0),
            "top_k": pick("top_k", 15),
            "top_p": pick("top_p", 1.0),
        }
        seed = getattr(voice, "seed", None)
        if seed is not None:
            out["seed"] = int(seed)
        return out

    def render(self, text: str, voice, lang: str = "zh", **kw):
        ref = getattr(voice, "ref", None)
        ref_text = getattr(voice, "ref_text", "") or ""
        if not ref:
            raise EngineError(f"音色 {voice.id} 没配置 ref（GPT-SoVITS 需要参考音频）")
        if not ref_text:
            raise EngineError(f"音色 {voice.id} 没配置 ref_text（必须和参考音频逐字一致）")

        payload = {
            "text": text,
            "text_lang": lang or "zh",
            "ref_audio_path": ref,
            "prompt_text": ref_text,
            "prompt_lang": lang or "zh",
            "media_type": "wav",
            **self._sampling(voice),
        }
        speed = kw.get("speed") or getattr(voice, "speed", None)
        if speed:
            payload["speed_factor"] = float(max(0.5, min(2.0, speed)))

        req = urllib.request.Request(
            f"{self.base_url}/tts",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read()[:300].decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                pass
            raise EngineError(f"gsv 合成失败 {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"gsv 合成失败: {e}") from e

        try:
            with wave.open(io.BytesIO(raw)) as w:
                sr = w.getframerate()
                pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
                ch = w.getnchannels()
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"gsv 返回的不是 wav：{e}") from e

        y = pcm.astype(np.float32) / 32768.0
        if ch > 1:
            y = y.reshape(-1, ch).mean(axis=1)
        # 服务端实际是 32k。这里自己重采样到 24k 并声明 24k，
        # 以后不管上层怎么处理都不会再出现"变慢变低"。
        if sr != self.caps.sr:
            n = int(len(y) * self.caps.sr / sr)
            y = np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y).astype(np.float32)
        yield y.astype(np.float32)
