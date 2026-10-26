"""云端引擎：火山引擎（豆包）声音复刻 2.0。

为什么需要它：
本地微调出来的 age12 能 1 秒出声、音色对，但**发音有硬伤**（听→qing、凉→lia、了含糊），
试过加数据、换精度、换参考音频都修不掉——这是 0.5B 级本地模型的上限。
云端复刻版用同一段样本训练，发音稳定得多，代价是要联网、按字符计费。

接口契约（2026-10 实测）：
    POST https://openspeech.bytedance.com/api/v3/tts/unidirectional
    Headers: X-Api-App-Key / X-Api-Access-Key / X-Api-Resource-Id: seed-icl-2.0
    Body:    {"user":{"uid":...},
              "req_params":{"text":...,"speaker":"S_xxx",
                            "audio_params":{"format":"wav","sample_rate":24000}}}
    返回:    多行 JSON，每行 data 是 base64 的 WAV 片段；
            首片带 RIFF 头但长度字段是 0xFFFFFFFF（流式），必须按 "data" 块自己切 PCM。

坑：
- 成功码有两种：0 和 20000000（后者 data 为 null，是结束帧）。
- resource id 必须是 `seed-icl-2.0`（声音复刻 2.0）；用 `seed-tts-2.0` 会报
  "resource ID is mismatched with speaker related resource"。
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request

import numpy as np

from .base import Caps, EngineError

DEFAULT_URL = "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
OK_CODES = {0, 20000000}


class CloudEngine:
    def __init__(
        self,
        app_id: str,
        access_key: str,
        resource_id: str = "seed-icl-2.0",
        name: str = "cloud",
        url: str = DEFAULT_URL,
        timeout: float = 60.0,
    ):
        self.name = name
        self.app_id = app_id
        self.access_key = access_key
        self.resource_id = resource_id
        self.url = url
        self.timeout = timeout
        self.caps = Caps(
            clone=True,
            instruct=False,
            langs=("zh", "en"),
            max_chars=1000,
            sr=24000,
            instruct_mode="none",
        )

    # ── 生命周期（云端没有"加载"，只检查凭据是否填了）────────────
    def load(self) -> None:
        if not self.app_id or not self.access_key:
            raise EngineError("云端引擎缺 app_id / access_key（config.toml 的 [engine] 段）")

    def unload(self) -> None:
        pass

    def close(self) -> None:
        pass

    # ── 合成 ─────────────────────────────────────────────────────────
    def render(self, text: str, voice, lang: str = "zh", **kw):
        speaker = getattr(voice, "speaker", None) or getattr(voice, "preset", None)
        if not speaker:
            raise EngineError(f"音色 {voice.id} 没配置 speaker（云端复刻得到的声音 ID）")

        sample_rate = int(getattr(voice, "sr", 0) or self.caps.sr)
        body = {
            "user": {"uid": "voxd"},
            "req_params": {
                "text": text,
                "speaker": speaker,
                "audio_params": {"format": "wav", "sample_rate": sample_rate},
            },
        }
        speed = kw.get("rate") or getattr(voice, "speed", 1.0) or 1.0
        if abs(float(speed) - 1.0) > 0.01:
            body["req_params"]["audio_params"]["speech_rate"] = int(round((float(speed) - 1) * 100))

        req = urllib.request.Request(
            self.url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Api-App-Key": self.app_id,
                "X-Api-Access-Key": self.access_key,
                "X-Api-Resource-Id": self.resource_id,
                "X-Api-Request-Id": f"voxd-{int(time.time() * 1000)}",
            },
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
            raise EngineError(f"云端合成失败 {e.code}: {detail}") from e
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"云端合成失败（网络？）: {e}") from e

        pcm = self._extract_pcm(raw)
        if not pcm:
            raise EngineError("云端返回里没有音频数据")

        y = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        yield y

    # ── 解析 ─────────────────────────────────────────────────────────
    @staticmethod
    def _extract_pcm(raw: bytes) -> bytes:
        """把多行 JSON 的 base64 分片拼成 PCM（跳过流式 WAV 头）。"""
        chunks: list[bytes] = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            code = obj.get("code")
            if code is not None and code not in OK_CODES:
                raise EngineError(f"云端返回错误 {code}: {obj.get('message', '')}")
            data = obj.get("data")
            if isinstance(data, str) and data:
                try:
                    chunks.append(base64.b64decode(data))
                except Exception:  # noqa: BLE001
                    continue
        blob = b"".join(chunks)
        if not blob:
            return b""
        idx = blob.find(b"data")
        pcm = blob[idx + 8 :] if idx >= 0 else blob[44:]
        return pcm[: len(pcm) // 2 * 2]
