"""请求与帧的唯一契约。

改这个文件 = 改协议。别处不许再定义一套结构——
宿主、引擎、测试都只看这里的类型。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Style(BaseModel):
    """语气。tags 是跨引擎的语义层，rate/pitch 是给不支持 instruct 的引擎兜底。"""

    model_config = ConfigDict(extra="forbid")

    tags: list[str] = Field(default_factory=list)
    intensity: float = Field(0.6, ge=0.0, le=1.0)
    rate: float = Field(1.0, ge=0.5, le=2.0)
    pitch: float = Field(0.0, ge=-6.0, le=6.0)


class VoiceRef(BaseModel):
    """音色的四种来源。只有一个字段是必填组：id / ref_audio / preset / design。"""

    model_config = ConfigDict(extra="forbid")

    id: str | None = None
    ref_audio: str | None = None
    ref_text: str | None = None
    preset: str | None = None
    design: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)

    @model_validator(mode="after")
    def _check_source(self):
        if not any((self.id, self.ref_audio, self.preset, self.design)):
            raise ValueError("voice 至少要给 id / ref_audio / preset / design 之一")
        if self.ref_audio and not self.ref_text:
            raise ValueError("ref_audio 必须同时给 ref_text（要和音频逐字对应）")
        return self


class SpeakRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=4000)
    character: str | None = None
    voice: str | VoiceRef | None = None
    style: Style = Field(default_factory=Style)
    lang: str = "zh"
    format: Literal["pcm", "wav", "ogg", "mp3"] = "pcm"
    sr: int = Field(24000, ge=8000, le=48000)
    priority: int = 0
    engine: str | None = None
    split: Literal["clause", "fast", "none"] = "clause"
    cache: bool = True


class Segment(BaseModel):
    """切分后的一小句。text 为空且 pause_ms>0 表示纯停顿。"""

    i: int
    text: str
    tags: list[str] = Field(default_factory=list)
    voice: str | None = None
    pause_ms: int = 0
    rate: float = 1.0
    pitch: float = 0.0
    intensity: float = 0.6


class EngineInfo(BaseModel):
    name: str
    loaded: bool
    clone: bool
    instruct: bool
    streaming: bool
    presets: list[str]
    langs: list[str]
    max_chars: int
    sr: int
    error: str | None = None
