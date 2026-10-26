"""远程引擎：把另一个进程（可以是别的 Python 版本、别的 venv、甚至别的机器）当成引擎用。

现在给 CosyVoice2 worker 用：POST {base}/say → wav 字节 → float32。
这样 voxd 的协议和宿主一行都不用改，引擎的依赖地狱被隔离在 worker 里。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request
import wave

import numpy as np

from .base import Caps, EngineError

# CosyVoice2 的指令必须以这个 token 收尾，否则模型会把它当成正文念出来。
INSTRUCT_END = "<|endofprompt|>"


class RemoteEngine:
    def __init__(self, base_url: str, name: str = "cosy", timeout: float = 300.0):
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.caps = Caps(
            clone=True,
            instruct=True,
            langs=("zh", "en", "ja", "ko", "yue"),
            max_chars=200,
            sr=24000,
            instruct_mode="imperative",
        )

    # ── 生命周期 ─────────────────────────────────────────────────────
    def _get(self, path: str) -> dict:
        with urllib.request.urlopen(f"{self.base_url}{path}", timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))

    def load(self) -> None:
        try:
            h = self._get("/health")
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"连不上 {self.name} worker（{self.base_url}）：{e}") from e
        if h.get("error"):
            raise EngineError(f"{self.name} worker 加载失败：{h['error']}")
        if not h.get("loaded"):
            # worker 支持懒加载：这里不算失败，只是第一条会慢
            return
        if h.get("sr"):
            self.caps = Caps(
                clone=True,
                instruct=True,
                langs=("zh", "en", "ja", "ko", "yue"),
                max_chars=200,
                sr=int(h["sr"]),
            )

    def close(self) -> None:
        pass

    # ── 合成 ─────────────────────────────────────────────────────────
    def render(self, text, *, voice, instruct=None, lang="zh", rate=1.0, pitch=0.0):
        payload = {
            "text": text,
            "instruct": (instruct or "用自然、口语化的语气说这句话") + INSTRUCT_END,
            "prompt": voice.ref_audio or "",
            # 注意：**不要**传 spk（已注册说话人）。实测走缓存会把 instruct 顶掉——
            # 时长从 0.2s/字 变成 0.49s/字、情绪指令失效，省下的 0.65s 不值。
            # worker 仍支持 spk，留给"纯克隆、不要情绪"的场景。
            "spk": "",
            "speed": float(max(0.9, min(1.1, rate))),
        }
        req = urllib.request.Request(
            f"{self.base_url}/say",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"content-type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
                sr = int(r.headers.get("X-Sr") or self.caps.sr)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            raise EngineError(f"{self.name} 合成失败 {e.code}：{detail}") from e
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"{self.name} 合成失败：{e}") from e

        try:
            with wave.open(io.BytesIO(data), "rb") as w:
                sr = w.getframerate()
                frames = w.readframes(w.getnframes())
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"{self.name} 返回的不是合法 wav：{e}") from e
        if not frames:
            raise EngineError(f"{self.name} 返回空音频")
        yield np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
