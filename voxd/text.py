"""文本归一化：把「聊天里能出现的东西」变成「TTS 敢念的东西」。

这一步枯燥但必须做：QQ 消息里混着 markdown、URL、表情占位符、转发标记，
直接喂给声学模型会读出「左方括号 图片 右方括号」。
"""

from __future__ import annotations

import re

# QQ / 桥接会塞进文本里的占位符。必须**按名单**剥，不能按「方括号里是中文」剥——
# 那会把 [开心] 这类语气标签一起吃掉。名单之外的 [xxx] 一律交给 markup 当标签解析。
_PLACEHOLDER_WORDS = (
    "图片|表情|语音|视频|文件|动画表情|拍一拍|红包|礼物|戳一戳|转发的消息|"
    "合并转发|聊天记录|名片|音乐|位置|链接|回复|引用|@全体成员"
)
_PLACEHOLDER = re.compile(rf"\[(?:{_PLACEHOLDER_WORDS})[^\]]*\]")
_FORWARD = re.compile(r"\[转发消息[^\]]*\]")
_AT = re.compile(r"\[@[^\]]{0,40}\]")

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE_CODE = re.compile(r"`([^`]*)`")
_MD_LINK = re.compile(r"\[([^\]]{1,80})\]\((?:[^)]{0,300})\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_MD_SYMBOL = re.compile(r"[*_~#>|^]+")
_LIST_MARK = re.compile(r"^\s*(?:[-+•]|\d+[.)])\s+", re.M)
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\u20E3]"
)
_SPACES = re.compile(r"[ \t\u3000]+")
_MULTI_NL = re.compile(r"\n{2,}")
_SENT_END = re.compile(r"[。！？!?；;…]")
_KEEP = re.compile(r"[^\u4e00-\u9fff\u3040-\u30ffa-zA-Z0-9，。！？、；：…—～·()（）\s]")

MAX_CHARS = 300


def strip_placeholders(text: str) -> str:
    text = _FORWARD.sub("", text)
    text = _AT.sub("", text)
    return _PLACEHOLDER.sub("", text)


def clean(text: str) -> str:
    """把一段（已剥掉占位符的）文本洗成适合朗读的样子。"""
    text = _FENCE.sub("", text)
    text = _INLINE_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _URL.sub("", text)
    text = _MD_SYMBOL.sub("", text)
    text = _LIST_MARK.sub("", text)
    text = _EMOJI.sub("", text)
    text = _KEEP.sub("", text)
    text = _SPACES.sub(" ", text)
    text = _MULTI_NL.sub("\n", text)
    text = text.strip(" \n，,、；;：:")
    return text


def clip(text: str, limit: int = MAX_CHARS) -> str:
    """按句末截断，不把句子切成半个字（超长是调用方的错，这里只兜底）。"""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for i in range(len(cut) - 1, max(0, limit - 60), -1):
        if _SENT_END.match(cut[i]):
            return cut[: i + 1]
    return cut
