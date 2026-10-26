"""引擎管理器 + 路由：能力匹配、降级、可读的错误。

路由只做一件事：给「这个音色 + 这个语气」挑一个真能干的引擎，
挑不到就说清楚缺什么——而不是抛一串 traceback。
"""

from __future__ import annotations

import threading

from .schema import EngineInfo, Style
from .engine.base import Caps, Engine, EngineError, Plan, tags_to_imperative, tags_to_instruct, tags_to_params


class Engines:
    """按需实例化、按需加载。同一时刻只有一个在跑（调度器保证），所以不需要线程池隔离。"""

    def __init__(self, specs: dict[str, callable], primary: str, fallbacks: list[str]):
        self._specs = specs
        self.primary = primary
        self.fallbacks = [f for f in fallbacks if f in specs]
        self._inst: dict[str, Engine] = {}
        self._errors: dict[str, str] = {}
        self._lock = threading.RLock()

    def order(self, hint: str | None = None) -> list[str]:
        seq = ([hint] if hint else []) + [self.primary] + self.fallbacks
        out: list[str] = []
        for n in seq:
            if n and n in self._specs and n not in out:
                out.append(n)
        return out

    def get(self, name: str) -> Engine:
        with self._lock:
            if name not in self._inst:
                if name not in self._specs:
                    raise EngineError(f"没有引擎「{name}」")
                try:
                    self._inst[name] = self._specs[name]()
                except EngineError:
                    raise
                except Exception as e:  # noqa: BLE001
                    # 构造函数就炸（缺模型/缺配置）也要变成可读的引擎错误，路由才能换下一个
                    raise EngineError(f"引擎 {name} 初始化失败：{e}") from e
            return self._inst[name]

    def ensure(self, name: str) -> Engine:
        """加载（幂等）。失败会记下原因并抛出可读错误。"""
        eng = self.get(name)
        if getattr(eng, "_voxd_loaded", False):
            return eng
        with self._lock:
            if getattr(eng, "_voxd_loaded", False):
                return eng
            try:
                eng.load()
            except Exception as e:  # noqa: BLE001
                self._errors[name] = str(e)
                raise EngineError(f"引擎 {name} 不可用：{e}") from e
            eng._voxd_loaded = True  # type: ignore[attr-defined]
            self._errors.pop(name, None)
            return eng

    def info(self) -> list[EngineInfo]:
        """列出**所有已注册**的引擎，而不只是路由顺序里的那几个。

        以前只遍历 order()（primary + fallbacks），导致 gsv / cloud / 插件引擎
        在 /v1/engines 里看不到——排查问题时很容易误判"没接上"。
        """
        out = []
        ordered = self.order()
        for name in ordered + [n for n in sorted(self._specs) if n not in ordered]:
            # 构造实例但不 load（构造不占显存），这样 /v1/engines 能显示真实能力
            try:
                eng = self.get(name)
            except Exception:  # noqa: BLE001
                eng = None
            caps = eng.caps if eng else Caps()
            out.append(
                EngineInfo(
                    name=name,
                    loaded=bool(getattr(eng, "_voxd_loaded", False)),
                    clone=caps.clone,
                    instruct=caps.instruct,
                    streaming=bool(getattr(caps, "streaming", False)),
                    presets=list(caps.presets),
                    langs=list(caps.langs),
                    max_chars=caps.max_chars,
                    sr=caps.sr,
                    error=self._errors.get(name),
                )
            )
        return out

    def close(self) -> None:
        for e in self._inst.values():
            try:
                e.close()
            except Exception:  # noqa: BLE001
                pass


def plan(engines: Engines, voice, style: Style, engine_hint: str | None = None) -> tuple[Engine, Plan]:
    """挑引擎；挑不到就抛 EngineError（带「缺什么」）。"""
    needs = voice.needs
    hint = engine_hint or getattr(voice, "engine", None)
    strict = bool(getattr(voice, "strict", False))
    reasons: list[str] = []
    order = engines.order(hint)
    if strict:
        # 定稿音色：只认它自己的引擎。宁可这一条不发，也不能换个声音发出去。
        order = [n for n in order if n == hint]
        if not order:
            raise EngineError(f"音色 {voice.id} 指定了 strict 引擎「{hint}」，但它没有注册")
    for name in order:
        try:
            # 加载失败 = 这个候选不可用，直接换下一个。
            # （以前只 get 不 ensure，cosy worker 没起来时会把 503 直接抛给宿主，
            #  明明 Piper 就在旁边能干活。）
            eng = engines.ensure(name)
        except EngineError as e:
            reasons.append(str(e))
            continue
        caps = eng.caps
        missing = [n for n in needs if not getattr(caps, n, False) and not (n == "preset" and caps.presets)]
        if missing:
            reasons.append(f"{name} 缺能力 {missing}")
            continue

        instruct = None
        rate = style.rate * voice.speed
        pitch = style.pitch
        degraded: list[str] = []
        if style.tags:
            supports = getattr(eng, "instruct_supported", None)
            can = caps.instruct and (supports(voice) if callable(supports) else True)
            if can:
                if getattr(caps, "instruct_mode", "description") == "imperative":
                    instruct = tags_to_imperative(style.tags, style.intensity)
                else:
                    instruct = tags_to_instruct(style.tags, style.intensity)
            else:
                rate, pitch = tags_to_params(style.tags, rate, pitch)
                degraded.append("tags→rate（引擎不支持语气）")
        if abs(rate - 1.0) > 0.08:
            degraded.append("rate 已钳制到 ±8%")
            rate = max(0.92, min(1.08, rate))
        return eng, Plan(engine=name, instruct=instruct, rate=rate, pitch=pitch, degraded=degraded)

    hint = f"（指定引擎 {engine_hint}）" if engine_hint else ""
    raise EngineError(f"没有引擎能满足这个请求{hint}：" + "；".join(reasons))
