"""测试与冒烟用的假引擎：不加载任何模型，按字数生成一段正弦波。

它存在的意义：12 条一致性测试必须能在**没有 GPU、没有权重**的机器上跑绿，
否则「泛用」就只是嘴上说说。
"""

from __future__ import annotations

import numpy as np

from .base import Caps


class StubEngine:
    name = "stub"
    caps = Caps(clone=True, instruct=True, presets=("stub-a", "stub-b"), max_chars=4000, sr=24000)

    def __init__(self):
        self.loaded = True

    def load(self) -> None:
        self.loaded = True

    def render(self, text, *, voice, instruct=None, lang="zh", rate=1.0, pitch=0.0):
        base = 220.0
        if instruct and "轻快" in instruct:
            base *= 1.15
        if instruct and "低沉" in instruct:
            base *= 0.85
        dur = min(4.0, 0.12 * max(1, len(text)) + 0.15)
        n = int(24000 * dur)
        t = np.arange(n, dtype=np.float32) / 24000.0
        env = np.minimum(1.0, np.minimum(t * 40, (dur - t) * 40)).clip(0, 1)
        yield (0.12 * np.sin(2 * np.pi * base * t) * env).astype(np.float32)

    def close(self) -> None:
        pass
