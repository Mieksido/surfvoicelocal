"""Qwen3-TTS 引擎（主引擎）。

同一个 checkpoint 只有一种能力，所以这里按**方法是否存在**来声明能力：
  · generate_voice_clone   → 克隆（ref_audio + ref_text）
  · generate_custom_voice  → 内置说话人 + instruct
  · generate_voice_design  → 用一句话描述生成音色

instruct 是否可用也是探测出来的：不同变体的签名不一样，
猜错会得到一条 TypeError，而不是「语气没生效」这种更难查的现象。
"""

from __future__ import annotations

import inspect

import numpy as np

from .base import Caps, EngineError

_LANG = {
    "zh": "Chinese",
    "en": "English",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
}
_DTYPE = {"bfloat16": "bfloat16", "float16": "float16", "float32": "float32"}

# CustomVoice 变体内置的 9 个音色（官方 README 列的）。模型 config 里有就用 config 的。
KNOWN_PRESETS = (
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
)


def _instruct_kwarg(fn) -> str | None:
    if fn is None:
        return None
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return None
    for cand in ("instruct", "instruction", "style", "emotion"):
        if cand in params:
            return cand
    return None



def static_caps(path: str) -> Caps:
    """只凭 checkpoint 目录名就能判断的能力。

    路由必须在**加载前**决定用哪个引擎，所以能力不能等 load() 才知道；
    这里的结论由 load() 再精修（比如 config 里真实的 speaker_map）。
    """
    tag = str(path).replace("\\", "/").lower()
    small = "0.6b" in tag or "0b6" in tag
    if "customvoice" in tag:
        return Caps(clone=False, instruct=not small, presets=KNOWN_PRESETS, langs=tuple(_LANG), max_chars=200)
    if "voicedesign" in tag:
        return Caps(design=True, langs=tuple(_LANG), max_chars=200)
    return Caps(clone=True, langs=tuple(_LANG), max_chars=200)


class Qwen3Engine:
    def __init__(
        self,
        path: str,
        device: str = "cuda:0",
        dtype: str = "bfloat16",
        attn: str = "sdpa",
        name: str = "qwen3",
    ):
        self.name = name
        self.path = path
        self.device = device
        self.dtype = dtype
        self.attn = attn
        self.model = None
        self.sr = 24000
        self._clone_instruct: str | None = None
        self._custom_instruct: str | None = None
        self._has_clone = False
        self._has_custom = False
        self._has_design = False
        self.presets: tuple[str, ...] = ()
        self.caps = static_caps(path)

    # ── 生命周期 ─────────────────────────────────────────────────────
    def load(self) -> None:
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
        except Exception as e:  # noqa: BLE001 - 依赖缺失要变成可读错误，不是 traceback
            raise EngineError(f"qwen-tts 未安装或 torch 不可用：{e}") from e

        dt = getattr(torch, _DTYPE.get(self.dtype, "bfloat16"))
        kwargs = {"device_map": self.device, "dtype": dt}
        if self.attn:
            kwargs["attn_implementation"] = self.attn
        try:
            self.model = Qwen3TTSModel.from_pretrained(self.path, **kwargs)
        except TypeError:
            kwargs.pop("attn_implementation", None)
            self.model = Qwen3TTSModel.from_pretrained(self.path, **kwargs)
        except Exception as e:  # noqa: BLE001
            raise EngineError(f"加载 {self.path} 失败：{e}") from e

        clone = getattr(self.model, "generate_voice_clone", None)
        custom = getattr(self.model, "generate_custom_voice", None)
        design = getattr(self.model, "generate_voice_design", None)
        # 注意：三个变体共用同一个 Python 类，方法全都存在——hasattr 区分不了 checkpoint。
        # 真正决定能力的是**权重训练出来的样子**，从目录名（官方命名）判断最可靠，
        # 猜错的后果是路由把请求派给一个合成不出东西的变体。
        tag = self.path.replace("\\", "/").lower()
        is_custom = "customvoice" in tag
        is_design = "voicedesign" in tag
        is_base = not is_custom and not is_design and ("base" in tag or clone is not None)
        self._has_clone = bool(clone) and is_base
        self._has_custom = bool(custom) and is_custom
        self._has_design = bool(design) and is_design
        self._clone_instruct = _instruct_kwarg(clone) if self._has_clone else None
        self._custom_instruct = _instruct_kwarg(custom) if self._has_custom else None
        # 0.6B 的 CustomVoice 会在内部**静默丢弃** instruct（官方代码：size in "0b6" → instruct=None）。
        # 这里如实声明能力，否则上层以为语气生效了，实际只是白传一个参数。
        inner = getattr(self.model, "model", self.model)  # 属性在内层模型上，不在 wrapper 上
        size = str(getattr(inner, "tts_model_size", "")).lower()
        if size in ("0b6", "0.6b", "0_6b"):
            self._custom_instruct = None

        presets: tuple[str, ...] = ()
        cfg = getattr(getattr(self.model, "model", None), "config", None) or getattr(self.model, "config", None)
        for attr in ("speakers", "speaker_map", "spk2id"):
            val = getattr(cfg, attr, None) if cfg is not None else None
            if val:
                presets = tuple(val.keys()) if isinstance(val, dict) else tuple(val)
                break
        if not presets and self._has_custom:
            presets = KNOWN_PRESETS
        if not presets:
            presets = self.caps.presets  # 静态判断的兜底（config 里没写 speaker_map 时）
        self.presets = presets

        self.caps = Caps(
            clone=self._has_clone,
            instruct=bool(self._clone_instruct or self._custom_instruct),
            presets=presets,
            langs=tuple(_LANG),
            max_chars=200,
            design=self._has_design,
        )
        self.sr = int(getattr(self.model, "sample_rate", 24000) or 24000)

        # 预热：把首条 1~2 秒的编译/显存分配成本，从用户的第一句话里挪走
        try:
            if self._has_custom and presets:
                self.model.generate_custom_voice(
                    text="嗯", language="Chinese", speaker=presets[0], **_opt(self._custom_instruct, "好的")
                )
        except Exception:  # noqa: BLE001
            pass

    def close(self) -> None:
        self.model = None
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass

    # ── 能力查询（路由用）───────────────────────────────────────────
    def instruct_supported(self, voice) -> bool:
        if voice.ref_audio:
            return self._clone_instruct is not None
        if voice.preset:
            return self._custom_instruct is not None
        if voice.design:
            return False
        return False

    # ── 合成 ─────────────────────────────────────────────────────────
    def render(self, text, *, voice, instruct=None, lang="zh", rate=1.0, pitch=0.0):
        if self.model is None:
            raise EngineError("模型未加载")
        language = _LANG.get(lang, "Chinese")

        if voice.ref_audio:
            kw = {"text": text, "language": language, "ref_audio": voice.ref_audio, "ref_text": voice.ref_text or ""}
            if instruct:
                kw.update(_opt(self._clone_instruct, instruct))
            wavs, sr = self.model.generate_voice_clone(**kw)
        elif voice.preset:
            kw = {"text": text, "language": language, "speaker": voice.preset}
            if instruct:
                kw.update(_opt(self._custom_instruct, instruct))
            wavs, sr = self.model.generate_custom_voice(**kw)
        elif voice.design:
            # 注意参数名是 instruct（描述放这里），不是 voice_description
            wavs, sr = self.model.generate_voice_design(
                text=text, language=language, instruct=voice.design
            )
        else:
            raise EngineError("音色缺少 ref_audio / preset / design，无法合成")

        if sr and int(sr) != self.sr:
            self.sr = int(sr)
        for w in wavs:
            yield np.asarray(w, dtype=np.float32).reshape(-1)


def _opt(kwname: str | None, value: str) -> dict:
    return {kwname: value} if kwname else {}
