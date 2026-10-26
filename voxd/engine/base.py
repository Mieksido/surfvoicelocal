"""引擎接口：能力表 + 一个生成器。

泛用的关键在这里——`Caps` 是**数据**，路由据它决定能不能接、要不要降级。
新增一个模型 = 新增一个实现这个 Protocol 的文件，别处一行不改。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, Protocol, runtime_checkable

import numpy as np


class EngineError(RuntimeError):
    pass


class Unsupported(Exception):
    """能力不够，交给路由换引擎。"""


@dataclass(frozen=True)
class Caps:
    clone: bool = False
    instruct: bool = False
    presets: tuple[str, ...] = ()
    langs: tuple[str, ...] = ("zh",)
    max_chars: int = 200
    sr: int = 24000
    design: bool = False
    # 引擎自己能控语速（不会改音高）。为 False 时上层才用重采样兜底。
    rate_control: bool = False
    # 语气怎么喂给它：description = 「语气轻快，句尾上扬」（描述式）；
    # imperative = 「用轻快的语气说这句话」（命令式，CosyVoice 这类 instruct 模型要这个）
    instruct_mode: str = "description"


@runtime_checkable
class Engine(Protocol):
    name: str
    caps: Caps

    def load(self) -> None: ...

    def render(
        self,
        text: str,
        *,
        voice,          # voices.Voice
        instruct: str | None,
        lang: str,
        rate: float,
        pitch: float,
    ) -> Iterator[np.ndarray]:
        """逐块产出 float32 波形，采样率 = caps.sr。"""
        ...

    def close(self) -> None: ...


# ── 语气标签 → 中文描述 ────────────────────────────────────────────
# 这是跨引擎的语义层：任何支持 instruct 的模型都吃这一张表。
TAG_TABLE: dict[str, str] = {
    "笑": "带着笑意，语气轻快",
    "开心": "语气轻快，句尾微微上扬",
    "兴奋": "语速偏快，音调偏高，情绪饱满",
    "温柔": "轻柔温和，语速稍慢",
    "小声": "压低声音，像在说悄悄话，气声偏多",
    "敷衍": "语气懒散，拖长音，缺乏兴致",
    "不耐烦": "语速偏快，句首重音，略显焦躁",
    "认真": "平稳清晰，停顿明确",
    "惊讶": "语调升高，句尾短促",
    "叹气": "带一声轻叹，气息下沉",
    "难过": "语调低沉，语速放慢",
    "平静": "平稳中性，没有明显起伏",
}

# 不支持 instruct 的引擎只能拿这两个数兜底，幅度必须小，否则立刻变「变声器味」。
_TAG_RATE = {"兴奋": 0.06, "不耐烦": 0.05, "敷衍": 0.04, "温柔": -0.05, "难过": -0.05, "叹气": -0.04}
_TAG_PITCH = {"兴奋": 0.5, "惊讶": 0.4, "难过": -0.5, "叹气": -0.3}


def tags_to_instruct(tags: list[str], intensity: float = 0.6) -> str | None:
    descs = [TAG_TABLE[t] for t in tags if t in TAG_TABLE]
    if not descs:
        return None
    line = "，".join(dict.fromkeys(descs))
    if intensity >= 0.85:
        line += "，情绪更强烈一些"
    elif intensity <= 0.35:
        line += "，克制一些"
    return line


# 命令式：CosyVoice 这类模型的 instruct 是「指令」，不是「描述」。
TAG_INSTRUCT: dict[str, str] = {
    "笑": "用带着笑意的语气说这句话",
    "开心": "用轻快、开心的语气说这句话",
    "兴奋": "用兴奋、语速稍快的语气说这句话",
    "温柔": "用温柔、轻柔的语气说这句话",
    "小声": "用压低声音、像说悄悄话的语气说这句话",
    "敷衍": "用敷衍、懒散、拖长音的语气说这句话",
    "不耐烦": "用不耐烦、略显焦躁的语气说这句话",
    "认真": "用认真、平稳、吐字清晰的语气说这句话",
    "惊讶": "用惊讶、语调升高的语气说这句话",
    "叹气": "用带着叹息、有些疲惫的语气说这句话",
    "难过": "用难过、低沉的语气说这句话",
    "平静": "用平静、没有起伏的语气说这句话",
}


def tags_to_imperative(tags: list[str], intensity: float = 0.6) -> str | None:
    lines = [TAG_INSTRUCT[t] for t in tags if t in TAG_INSTRUCT]
    if not lines:
        return None
    line = "，".join(dict.fromkeys(lines))
    if intensity >= 0.85:
        line = line.replace("说这句话", "说这句话，情绪更强烈一些")
    elif intensity <= 0.35:
        line = line.replace("说这句话", "说这句话，克制一些")
    return line


def tags_to_params(tags: list[str], rate: float, pitch: float) -> tuple[float, float]:
    r = rate + sum(_TAG_RATE.get(t, 0.0) for t in tags)
    p = pitch + sum(_TAG_PITCH.get(t, 0.0) for t in tags)
    return max(0.92, min(1.08, r)), max(-2.0, min(2.0, p))


@dataclass
class Plan:
    """路由结果：用谁、怎么表达、降级了什么。"""

    engine: str
    instruct: str | None = None
    rate: float = 1.0
    pitch: float = 0.0
    degraded: list[str] = field(default_factory=list)
