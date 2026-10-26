"""Piper 引擎：CPU 兜底，永远是最后一道防线。

它不能克隆、不能控情绪，但**一定能出声**——主引擎挂了、权重没下完、
显存被游戏占了，它都能顶上。这就是「泛用」的最后保险。
"""

from __future__ import annotations

import io
import wave

import numpy as np

from .base import Caps, EngineError


class PiperEngine:
    name = "piper"
    caps = Caps(clone=False, instruct=False, langs=("zh", "en"), sr=22050, rate_control=True)

    def __init__(self, model_path: str, config_path: str | None = None):
        self.model_path = model_path
        self.config_path = config_path or (model_path + ".json")
        self.voice = None
        self.sr = 22050

    def load(self) -> None:
        try:
            from piper import PiperVoice
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"piper 未安装：{e}") from e
        try:
            self.voice = PiperVoice.load(self.model_path, config_path=self.config_path)
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"加载 piper 模型失败（{self.model_path}）：{e}") from e
        cfg = getattr(self.voice, "config", None)
        self.sr = int(getattr(cfg, "sample_rate", 0) or getattr(self.voice, "sample_rate", 22050))

    def close(self) -> None:
        self.voice = None

    def _chunks(self, text: str, rate: float = 1.0):
        v = self.voice
        if hasattr(v, "synthesize"):  # piper 1.8+
            # length_scale 越大越慢；用它控速不会像重采样那样连带改音高。
            cfg = None
            try:
                from piper import SynthesisConfig

                cfg = SynthesisConfig(length_scale=1.0 / max(0.5, min(2.0, rate)))
            except Exception:  # noqa: BLE001 - 老版本没有这个类，退回原速
                cfg = None
            for ch in v.synthesize(text, syn_config=cfg):
                fa = getattr(ch, "audio_float_array", None)
                if fa is not None:
                    yield np.asarray(fa, dtype=np.float32).reshape(-1)
                    continue
                ib = getattr(ch, "audio_int16_bytes", None) or getattr(ch, "audio_int16_array", None)
                if isinstance(ib, (bytes, bytearray)):
                    yield np.frombuffer(ib, dtype="<i2").astype(np.float32) / 32767.0
                elif ib is not None:
                    yield np.asarray(ib, dtype=np.float32) / 32767.0
            return
        # 老接口：写进 wav 再读回来
        buf = io.BytesIO()
        v.synthesize_wav(text, buf)
        buf.seek(0)
        with wave.open(buf, "rb") as w:
            self.sr = w.getframerate()
            data = w.readframes(w.getnframes())
        yield np.frombuffer(data, dtype="<i2").astype(np.float32) / 32767.0

    def render(self, text, *, voice, instruct=None, lang="zh", rate=1.0, pitch=0.0):
        if self.voice is None:
            raise EngineError("piper 未加载")
        yield from self._chunks(text, rate)
