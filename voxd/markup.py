"""内联标记 → Segment 列表。

语法（全部可选，不写就是一段平文本）：
    [开心]            语气标签，作用于**其后**的文字，遇到下一个标签即替换
    [voice:b]         切换音色
    [pause:300]       插入 300ms 静音

这是三个宿主唯一共享的东西：它们只管把 LLM 原文丢过来。
"""

from __future__ import annotations

import re

from . import text as T
from .schema import Segment

_TAG = re.compile(r"\[([^\[\]\n]{1,40})\]")
_SENT = re.compile(r"(?<=[。！？!?；;…])|(?<=\n)")
_COMMA = re.compile(r"(?<=[，,、])")
# 只有标点、没有一个可读字符的「句子」不发声音（否则会合成出一声怪响）。
_SPEAKABLE = re.compile(r"[\u4e00-\u9fff\u3040-\u30ffA-Za-z0-9]")

# 太短的碎片单独成句会让韵律变怪（"啊。"听完一顿），合并到下一段。
_MIN_PIECE = 4
# fast 模式下，第一段超过这个长度就找逗号切开，先出首包。
_FAST_CUT = 16


def _split_sentences(s: str) -> list[str]:
    return [p for p in _SENT.split(s) if p and p.strip()]


def _split_long(s: str, limit: int) -> list[str]:
    if len(s) <= limit:
        return [s]
    out: list[str] = []
    for part in _COMMA.split(s):
        if not part:
            continue
        if len(part) > limit:
            for i in range(0, len(part), limit):
                out.append(part[i : i + limit])
        else:
            out.append(part)
    return out


def _merge_short(pieces: list[str]) -> list[str]:
    out: list[str] = []
    buf = ""
    for p in pieces:
        buf += p
        if len(buf.strip()) >= _MIN_PIECE:
            out.append(buf)
            buf = ""
    if buf:
        if out:
            out[-1] += buf
        else:
            out.append(buf)
    return out


def _chunk(s: str, mode: str, limit: int) -> list[str]:
    if mode == "none" or not s:
        return [s] if s else []
    pieces = _merge_short(_split_sentences(s))
    pieces = [q for p in pieces for q in _split_long(p, limit)]
    if mode == "fast" and pieces and len(pieces[0]) > _FAST_CUT:
        head = pieces[0]
        m = _COMMA.search(head, _FAST_CUT // 2)
        if m and m.end() < len(head) - 2:
            pieces[0:1] = [head[: m.end()], head[m.end() :]]
    return [p for p in pieces if p.strip()]


def parse(raw: str, *, split: str = "clause", limit: int = 60) -> list[Segment]:
    """把原文变成可逐句合成的 Segment 列表。"""
    src = T.strip_placeholders(raw)

    # 第一遍：切出 (文本, 标签列表, 音色, 前置停顿)
    pieces: list[tuple[str, list[str], str | None, int]] = []
    tags: list[str] = []
    voice: str | None = None
    pause = 0
    pos = 0
    for m in _TAG.finditer(src):
        chunk = src[pos : m.start()]
        if chunk:
            pieces.append((chunk, list(tags), voice, pause))
            pause = 0
        pos = m.end()
        body = m.group(1).strip()
        low = body.lower()
        if low.startswith("voice:"):
            voice = body[6:].strip() or None
        elif low.startswith("pause:"):
            try:
                pause = max(0, min(10000, int(float(body[6:].strip()))))
            except ValueError:
                pause = 0
        else:
            tags = [body]
    tail = src[pos:]
    if tail:
        pieces.append((tail, list(tags), voice, pause))
    elif tags and pieces and not pieces[-1][1]:
        # 标签写在句末（"来了来了[笑]"）时，按"作用于其后"会落空——
        # 这种情况下把它补给最后一段，别让模型白标一个情绪。
        last = pieces[-1]
        pieces[-1] = (last[0], list(tags), last[2], last[3])

    # 第二遍：洗文本 + 切句
    out: list[Segment] = []
    for chunk, ptags, pvoice, ppause in pieces:
        body = T.clean(chunk)
        if not body or not _SPEAKABLE.search(body):
            if ppause and out:
                out[-1].pause_ms = min(10000, out[-1].pause_ms + ppause)
            continue
        for i, part in enumerate(_chunk(body, split, limit)):
            out.append(
                Segment(
                    i=0,
                    text=part,
                    tags=list(ptags),
                    voice=pvoice,
                    pause_ms=ppause if i == 0 else 0,
                )
            )
    for i, seg in enumerate(out):
        seg.i = i
    return out
