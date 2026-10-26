"""音色注册表：目录即注册，改文件即生效，不用重启。

voices/<id>/ref.wav + voice.json
voice.json 支持的字段（全可选，除 ref 相关）：
    desc            这句话是用来「重新生成这个音色」的种子，不是给人看的注释
    ref_audio       默认 ref.wav
    ref_text        参考音频的逐字文本，克隆必需
    preset          引擎内置说话人
    design          描述生成（需要 design 引擎）
    speed           默认语速
    default_style   {"tags":[...], "intensity":0.6, "rate":1.0, "pitch":0.0}
    engine          建议引擎（不强制）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .schema import VoiceRef


@dataclass
class Voice:
    id: str
    ref_audio: str | None = None
    ref_text: str | None = None
    preset: str | None = None
    design: str | None = None
    desc: str = ""
    speed: float = 1.0
    default_style: dict = field(default_factory=dict)
    engine: str | None = None
    # GPT-SoVITS 专用：参考音频（3~10 秒）+ 逐字文本 + 采样参数
    ref: str | None = None
    temperature: float | None = None
    top_k: int | None = None
    top_p: float | None = None
    seed: int | None = None
    speaker: str | None = None
    # 只允许用 engine 指定的引擎；失败就报错，绝不换声音（定稿音色必须开这个）
    strict: bool = False
    # 这个音色希望怎么切句（None = 听请求的）。定稿音色用 none：整句一口气合成。
    split: str | None = None
    # 双引擎孪生：同一个"人"，快引擎和情绪引擎各一个音色
    cosy_twin: str | None = None
    fast_twin: str | None = None

    @property
    def needs(self) -> set[str]:
        """这个音色要求引擎具备哪种能力。"""
        if self.ref_audio:
            return {"clone"}
        if self.preset:
            return {"preset"}
        if self.design:
            return {"design"}
        return set()


class VoiceError(Exception):
    pass


class VoiceStore:
    def __init__(self, root: Path, default_id: str = ""):
        self.root = Path(root)
        self.default_id = default_id
        self._voices: dict[str, Voice] = {}
        self._sig: tuple = ()
        self.refresh(force=True)

    # ── 目录签名：新增/删除/改文件都会变 ──────────────────────────────
    def _signature(self) -> tuple:
        sig = []
        try:
            for e in sorted(self.root.iterdir(), key=lambda p: p.name):
                if not e.is_dir():
                    continue
                try:
                    files = tuple(
                        sorted((f.name, f.stat().st_mtime_ns) for f in e.iterdir() if f.is_file())
                    )
                except OSError:
                    continue
                sig.append((e.name, files))
        except FileNotFoundError:
            pass
        return tuple(sig)

    def refresh(self, force: bool = False) -> None:
        sig = self._signature()
        if not force and sig == self._sig:
            return
        voices: dict[str, Voice] = {}
        for entry in sig:
            vid, files = entry
            meta = {}
            for name, _ in files:
                if name.endswith(".json"):
                    try:
                        meta = json.loads((self.root / vid / name).read_text("utf-8"))
                    except (OSError, ValueError):
                        meta = {}
                    break
            ref = meta.get("ref_audio") or "ref.wav"
            ref_path = self.root / vid / ref
            if not ref_path.is_file():
                ref_path = None
                ref = None
            gsv_ref = meta.get("ref")
            if gsv_ref:
                cand = Path(gsv_ref)
                if not cand.is_absolute():
                    cand = (self.root / vid / gsv_ref).resolve()
                    if not cand.is_file():
                        cand = (self.root.parent / gsv_ref).resolve()
                gsv_ref = str(cand) if cand.is_file() else None
            voices[vid] = Voice(
                id=vid,
                ref=gsv_ref,
                temperature=meta.get("temperature"),
                top_k=meta.get("top_k"),
                top_p=meta.get("top_p"),
                seed=meta.get("seed"),
                speaker=meta.get("speaker"),
                strict=bool(meta.get("strict", False)),
                split=meta.get("split"),
                ref_audio=str(ref_path) if ref_path else None,
                ref_text=meta.get("ref_text"),
                preset=meta.get("preset"),
                design=meta.get("design"),
                desc=meta.get("desc", ""),
                speed=float(meta.get("speed", 1.0)),
                default_style=meta.get("default_style") or {},
                engine=meta.get("engine"),
                cosy_twin=meta.get("cosy_twin"),
                fast_twin=meta.get("fast_twin"),
            )
        self._voices = voices
        self._sig = sig

    # ── 查询 ─────────────────────────────────────────────────────────
    def all(self) -> dict[str, Voice]:
        self.refresh()
        return self._voices

    def get(self, vid: str) -> Voice:
        self.refresh()
        v = self._voices.get(vid)
        if not v:
            known = "、".join(sorted(self._voices)) or "（空）"
            raise VoiceError(f"没有音色「{vid}」，可用：{known}")
        return v

    def resolve(self, ref: str | VoiceRef | None) -> Voice:
        """把四种来源统一成一个 Voice。显式字段覆盖注册表里的值。"""
        if ref is None:
            if not self.default_id:
                raise VoiceError("请求没指定音色，config 里也没有 [voice].default")
            return self.get(self.default_id)
        if isinstance(ref, str):
            return self.get(ref)

        base = self.get(ref.id) if ref.id else Voice(id="adhoc")
        v = Voice(
            id=base.id if ref.id else "adhoc",
            ref_audio=ref.ref_audio or base.ref_audio,
            ref_text=ref.ref_text or base.ref_text,
            preset=ref.preset or base.preset,
            design=ref.design or base.design,
            desc=base.desc,
            speed=ref.speed if ref.speed != 1.0 else base.speed,
            default_style=base.default_style,
            engine=base.engine,
        )
        if v.ref_audio and not os.path.isfile(v.ref_audio):
            raise VoiceError(f"参考音频不存在：{v.ref_audio}")
        if v.ref_audio and not v.ref_text:
            raise VoiceError(f"音色 {v.id} 有 ref_audio 但缺 ref_text，克隆时会跑音")
        return v

    def summary(self) -> list[dict]:
        return [
            {
                "id": v.id,
                "desc": v.desc,
                "needs": sorted(v.needs),
                "has_ref": bool(v.ref_audio),
            }
            for v in sorted(self.all().values(), key=lambda x: x.id)
        ]
