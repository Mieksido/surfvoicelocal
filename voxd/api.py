"""HTTP 门面。协议只有四个端点，两种输出形态（流 / 文件）。

所有合成都在 Scheduler 的唯一工作线程里跑；这里只负责解析、鉴权、编码帧。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import tomllib
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import codec, markup
from .cache import DiskCache, make_key
from .engine.base import Caps, EngineError
from .engine.piper import PiperEngine
from .engine.remote import RemoteEngine
from .engine.gsv import GsvEngine
from .engine.cloud import CloudEngine
from .engine.http_plugin import HttpEngine
from .engine.stub import StubEngine
from .router import Engines, plan
from .scheduler import Job, QueueFull, Scheduler
from .schema import SpeakRequest, Style
from .voices import Voice, VoiceError, VoiceStore

ROOT = Path(__file__).resolve().parent.parent
MEDIA = {
    "pcm": "application/octet-stream",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "mp3": "audio/mpeg",
}


class Config:
    def __init__(self, path: Path):
        self.path = Path(path)
        raw = tomllib.loads(self.path.read_text("utf-8")) if self.path.is_file() else {}
        s = raw.get("server", {})
        e = raw.get("engine", {})
        v = raw.get("voice", {})
        li = raw.get("limits", {})
        c = raw.get("cache", {})
        f = raw.get("ffmpeg", {})
        base = self.path.parent
        self.host = s.get("host", "127.0.0.1")
        self.port = int(s.get("port", 8765))
        self.token = str(s.get("token", "") or "")
        self.primary = e.get("primary", "qwen3")
        self.fallbacks = list(e.get("fallbacks") or [])
        self.qwen3_path = str((base / e.get("qwen3_path", "models/Qwen3-TTS-12Hz-0.6B-CustomVoice")).resolve())
        self.qwen3_base_path = str(
            (base / e.get("qwen3_base_path", "models/Qwen3-TTS-12Hz-0.6B-Base")).resolve()
        )
        self.device = e.get("device", "cuda:0")
        self.dtype = e.get("dtype", "bfloat16")
        self.attn = e.get("attn", "sdpa")
        self.piper_model = str((base / e.get("piper_model", "models/zh_CN-huayan-medium.onnx")).resolve())
        self.cosy_url = str(e.get("cosy_url", "http://127.0.0.1:8766"))
        self.gsv_url = str(e.get("gsv_url", "http://127.0.0.1:9880"))
        # 云端（火山声音复刻 2.0）：直播/成片用，质量优先
        self.cloud_app_id = str(e.get("cloud_app_id", "") or "")
        self.cloud_access_key = str(e.get("cloud_access_key", "") or "")
        self.cloud_resource = str(e.get("cloud_resource", "seed-icl-2.0"))
        # 配置驱动的外部引擎：[engines.<name>] kind = "http"
        self.plugins = dict(raw.get("engines") or {})
        self.cosy_max_chars = int(e.get("cosy_max_chars", 0) or 0)
        self.gap_ms = int(e.get("gap_ms", 80))
        # 启动后在后台"空跑"一次，避免用户第一条请求吃到 4~6 秒的模型预热
        self.warmup = list(e.get("warmup") or [])
        self.default_voice = str(v.get("default", "") or "")
        self.characters = dict(v.get("characters") or {})
        self.max_chars = int(li.get("max_chars", 300))
        self.queue_max = int(li.get("queue_max", 64))
        self.cache_dir = (base / c.get("dir", "cache")).resolve()
        self.cache_mb = int(c.get("max_mb", 1024))
        self.ffmpeg_path = str(f.get("path", "") or "")
        self.voices_dir = (base / "voices").resolve()


def _plugin_specs(cfg: Config) -> dict[str, Any]:
    """把 config.toml 的 [engines.<name>] 变成引擎工厂。

    这样加一个新 TTS（别的云、自研模型、别人的服务）只需要改配置，
    不用动 voxd 的代码。
    """
    out: dict[str, Any] = {}
    for name, spec in (cfg.plugins or {}).items():
        if not isinstance(spec, dict):
            continue
        kind = str(spec.get("kind", "http")).lower()
        if kind != "http":
            continue
        url = str(spec.get("url", "") or "")
        if not url:
            continue
        caps = Caps(
            clone=bool(spec.get("clone", True)),
            instruct=bool(spec.get("instruct", False)),
            langs=tuple(spec.get("langs") or ("zh",)),
            max_chars=int(spec.get("max_chars", 200)),
            sr=int(spec.get("sr", 24000)),
        )
        headers = dict(spec.get("headers") or {})
        timeout = float(spec.get("timeout", 120))
        out[name] = (
            lambda n=name, u=url, c=caps, h=headers, t=timeout: HttpEngine(u, name=n, caps=c, headers=h, timeout=t)
        )
    return out


def _specs(cfg: Config) -> dict[str, Any]:
    base = {
        "stub": StubEngine,
        "piper": lambda: PiperEngine(cfg.piper_model),
        "gsv": lambda: GsvEngine(cfg.gsv_url),
        "cloud": lambda: CloudEngine(cfg.cloud_app_id, cfg.cloud_access_key, cfg.cloud_resource),
    }
    base.update(_plugin_specs(cfg))  # 插件同名可覆盖内置（显式配置优先）
    return base


class Synth:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.voices = VoiceStore(cfg.voices_dir, cfg.default_voice)
        self.engines = Engines(_specs(cfg), cfg.primary, cfg.fallbacks)
        self.cache = DiskCache(cfg.cache_dir, cfg.cache_mb * 1024 * 1024)
        self.ffmpeg = codec.find_ffmpeg(cfg.ffmpeg_path)

    # ── 解析 ─────────────────────────────────────────────────────────
    def voice_of(self, req: SpeakRequest, seg) -> Voice:
        ref = req.voice
        if seg.voice:  # [voice:b] 优先
            ref = seg.voice
        if ref is None and req.character:
            ref = self.cfg.characters.get(req.character)
            if not ref:
                raise VoiceError(f"角色「{req.character}」没有配 [voice.characters] 映射")
        return self.voices.resolve(ref)

    def style_of(self, req: SpeakRequest, seg, voice: Voice) -> Style:
        ds = voice.default_style or {}
        tags = seg.tags or req.style.tags or list(ds.get("tags") or [])
        intensity = req.style.intensity if req.style.tags else float(ds.get("intensity", req.style.intensity))
        rate = req.style.rate if req.style.rate != 1.0 else float(ds.get("rate", 1.0))
        pitch = req.style.pitch if req.style.pitch != 0.0 else float(ds.get("pitch", 0.0))
        return Style(tags=tags, intensity=intensity, rate=rate, pitch=pitch)

    def _voice_fingerprint(self, req: SpeakRequest) -> str:
        """把影响音色的音色级参数摘出来，作为缓存键的一部分。

        踩过的坑：固定了 temperature/seed 之后，同文本仍命中旧采样参数的缓存，
        结果"改了配置却听到老声音"。
        """
        vid = req.voice if isinstance(req.voice, str) else None
        if not vid and req.character:
            # 走角色名时也要解析出真实音色 id：否则换了 mappings 之后
            # 缓存键不变，会命中上一个音色的音频。
            vid = self.cfg.characters.get(req.character)
        if not vid:
            return ""
        try:
            v = self.voices.get(vid)
        except Exception:  # noqa: BLE001
            return vid
        return "|".join(
            str(x)
            for x in (
                vid, v.engine, v.ref, v.temperature, v.top_k, v.top_p, v.seed,
                getattr(v, "cosy_twin", None), getattr(v, "fast_twin", None),
            )
        )

    def cache_key(self, req: SpeakRequest) -> str:
        vkey = req.voice if isinstance(req.voice, str) else (req.voice.model_dump() if req.voice else None)
        return make_key(
            "v1",
            self.engines.primary,
            req.engine,
            self._voice_fingerprint(req),  # 音色参数变了（采样/种子/参考），旧缓存必须失效
            req.character,
            vkey,
            req.text,
            req.style.model_dump(),
            req.split,
            req.sr,
            self.cfg.cosy_max_chars,
        )

    # ── 单句合成 ─────────────────────────────────────────────────────
    def _pick_voice(self, voice: Voice, text: str, style: Style) -> Voice:
        """双引擎分流：同一个人，短情绪句走 cosy 孪生，其余走快孪生。

        `[engine].cosy_max_chars = 0` 即关闭。孪生没配 / 解析失败时保持原音色。
        """
        limit = self.cfg.cosy_max_chars
        if limit <= 0:
            return voice
        want_cosy = bool(style.tags) and len(text) <= limit
        twin = voice.cosy_twin if want_cosy else voice.fast_twin
        if not twin:
            return voice
        try:
            return self.voices.resolve(twin)
        except VoiceError:
            return voice

    def _hint(self, text: str, style: Style, hint: str | None) -> str | None:
        """双引擎分流：带语气标签的短句优先走 CosyVoice（情绪），其余走 Piper（快）。

        `[engine].cosy_max_chars = 0` 即关闭。worker 没起来时路由会自动退回 Piper。
        """
        if hint or not self.cfg.cosy_max_chars or not style.tags:
            return hint
        if len(text) <= self.cfg.cosy_max_chars:
            return "cosy"
        return hint

    def warm_background(self, names: list[str]) -> None:
        """启动后自动空跑一条，把算子预热掉。

        没有它的话，服务刚起来的第一条会慢 4~6 秒（不是模型加载，是 CUDA 预热），
        用户很容易以为"变慢了"。names 里的引擎还没起来就一直等。
        """
        deadline = time.time() + 240
        while time.time() < deadline:
            try:
                voice = self.voices.get(self.cfg.default_voice) if self.cfg.default_voice else None
                if voice is None:
                    return
                self._one("预热一下。", voice, Style(), "zh", None, 24000)
                return
            except Exception:  # noqa: BLE001
                time.sleep(3)

    def _split_for(self, req: SpeakRequest) -> str:
        """按请求里解析出的音色决定切句方式。定稿音色要求整句合成。"""
        vid = req.voice if isinstance(req.voice, str) else None
        if not vid and req.character:
            vid = self.cfg.characters.get(req.character)
        vid = vid or self.cfg.default_voice
        try:
            v = self.voices.get(vid)
            return getattr(v, "split", None) or req.split
        except Exception:  # noqa: BLE001
            return req.split

    def _log_request(self, voice, engine: str, text: str, ms: int, degraded: list[str]) -> None:
        """每次合成记一行。排查"声音怎么变了"时先看 logs/requests.jsonl。"""
        try:
            import json as _json
            import time as _time

            path = self.cfg.cache_dir.parent / "logs" / "requests.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                f.write(
                    _json.dumps(
                        {
                            "t": _time.strftime("%Y-%m-%dT%H:%M:%S"),
                            "voice": getattr(voice, "id", ""),
                            "engine": engine,
                            "chars": len(text),
                            "ms": ms,
                            "degraded": degraded,
                        },
                        ensure_ascii=False,
                    )
                    + chr(10)
                )
        except Exception:  # noqa: BLE001
            pass

    def _one(self, text: str, voice: Voice, style: Style, lang: str, hint: str | None, sr: int):
        eng, pl = plan(self.engines, voice, style, self._hint(text, style, hint))
        chunks = list(
            eng.render(text, voice=voice, instruct=pl.instruct, lang=lang, rate=pl.rate, pitch=pl.pitch)
        )
        if not chunks:
            return b"", pl.degraded, eng.name
        y = np.concatenate(chunks) if len(chunks) > 1 else chunks[0]
        pcm = codec.to_pcm16(y)
        # 语气用 resample 兜底只在两种情况下做：引擎没有 instruct；或用户显式要变速
        if (
            abs(pl.rate - 1.0) > 0.003
            and not eng.caps.rate_control
            and (not pl.instruct or abs(style.rate - 1.0) > 0.03)
        ):
            pcm = codec.apply_rate(pcm, eng.caps.sr, pl.rate)
        if eng.caps.sr != sr:
            pcm = codec.resample(pcm, eng.caps.sr, sr)
        self._log_request(voice, eng.name, text, int(len(pcm) / 2 / sr * 1000), pl.degraded)
        return pcm, pl.degraded, eng.name

    # ── 整条任务（跑在 worker 线程里）────────────────────────────────
    def run(self, job: Job, req: SpeakRequest) -> None:
        t_start = time.perf_counter()
        # 音色可以有自己的切句偏好：定稿音色必须整句一口气合成，
        # 否则同一句话被切成两段分别生成，拼起来像两个人（踩过）。
        split_mode = self._split_for(req)
        segs = markup.parse(req.text, split=split_mode, limit=4000 if split_mode == "none" else 60)
        if not segs:
            job.emit({"type": "start", "sr": req.sr, "format": "pcm", "engine": self.engines.primary, "segments": 0})
            job.emit({"type": "end", "dur_ms": 0, "cached": False})
            return

        key = self.cache_key(req)
        if req.cache:
            hit = self.cache.get(key)
            if hit:
                pcm, meta = hit
                job.emit(
                    {
                        "type": "start",
                        "sr": req.sr,
                        "format": "pcm",
                        "engine": self.engines.primary,
                        "segments": len(meta),
                        "cached": True,
                    }
                )
                for m in meta:
                    job.emit(
                        {
                            "type": "seg",
                            "i": m["i"],
                            "text": m["text"],
                            "tags": m["tags"],
                            "voice": m["voice"],
                            "t0": m["t0"],
                            "t1": m["t1"],
                            "pause_ms": m["pause_ms"],
                            "cached": True,
                            "pcm": pcm[m["off"] : m["off"] + m["n"]],
                        }
                    )
                job.emit({"type": "end", "dur_ms": codec.duration_ms(pcm, req.sr), "cached": True})
                return

        first = self.voice_of(req, segs[0])
        job.emit(
            {
                "type": "start",
                "sr": req.sr,
                "format": "pcm",
                "engine": self.engines.primary,
                "voice": first.id,
                "segments": len(segs),
            }
        )

        parts: list[bytes] = []
        meta: list[dict] = []
        off = 0
        degraded: set[str] = set()

        for idx, seg in enumerate(segs):
            if job.cancel.is_set():
                job.emit({"type": "cancelled", "dur_ms": off * 1000 // (2 * req.sr)})
                return
            voice = self.voice_of(req, seg)
            style = self.style_of(req, seg, voice)
            voice = self._pick_voice(voice, seg.text, style)

            if not seg.text.strip():  # 纯停顿
                pcm = codec.silence(seg.pause_ms, req.sr)
            else:
                eng_name = ""
                try:
                    pcm, notes, eng_name = self._one(seg.text, voice, style, req.lang, req.engine, req.sr)
                except VoiceError:
                    raise
                except Exception as e:  # noqa: BLE001
                    raise EngineError(f"第 {seg.i} 句合成失败：{e}") from e
                degraded.update(notes)
                if seg.pause_ms:
                    pcm += codec.silence(seg.pause_ms, req.sr)
            # 句间留白：以前 gap_ms 只写在 config 里、代码从不读，多句回复是一句接一句硬拼的。
            if (
                self.cfg.gap_ms > 0
                and idx < len(segs) - 1
                and seg.text.rstrip().endswith(("。", "！", "？", "!", "?", "；", ";", "…"))
            ):
                pcm += codec.silence(self.cfg.gap_ms, req.sr)

            t0 = off * 1000 // (2 * req.sr)
            t1 = t0 + codec.duration_ms(pcm, req.sr)
            parts.append(pcm)
            meta.append(
                {
                    "i": seg.i,
                    "text": seg.text,
                    "tags": seg.tags,
                    "voice": voice.id,
                    "t0": t0,
                    "t1": t1,
                    "off": off,
                    "n": len(pcm),
                    "pause_ms": seg.pause_ms,
                    "engine": eng_name,
                }
            )
            off += len(pcm)
            job.emit(
                {
                    "type": "seg",
                    "i": seg.i,
                    "text": seg.text,
                    "tags": seg.tags,
                    "voice": voice.id,
                    "t0": t0,
                    "t1": t1,
                    "pause_ms": seg.pause_ms,
                    "engine": eng_name,
                    "pcm": pcm,
                }
            )

        pcm = b"".join(parts)
        dur = codec.duration_ms(pcm, req.sr)
        job.emit(
            {
                "type": "end",
                "dur_ms": dur,
                "cached": False,
                "degraded": sorted(degraded),
                "elapsed_ms": int((time.perf_counter() - t_start) * 1000),
            }
        )
        if req.cache:
            self.cache.put(key, pcm, meta)

    def preload(self) -> None:
        """预热**默认音色要用的引擎**，而不是盲猜 primary。

        默认音色是 Piper 时去加载 20 秒的 Qwen3 只是白占 2.2G 显存——
        聊天根本不会走到它。
        """
        name = self.engines.primary
        try:
            if self.voices.default_id:
                name = getattr(self.voices.resolve(self.voices.default_id), "engine", None) or name
        except Exception:  # noqa: BLE001
            pass
        try:
            self.engines.ensure(name)
        except Exception:  # noqa: BLE001 - 预加载失败留给 /health 显示
            pass


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def create_app(cfg: Config) -> FastAPI:
    synth = Synth(cfg)
    sched = Scheduler(cfg.queue_max)
    started = time.time()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched.start()
        asyncio.get_running_loop().run_in_executor(None, synth.preload)
        if cfg.warmup:
            asyncio.get_running_loop().run_in_executor(None, synth.warm_background, cfg.warmup)
        yield
        await sched.stop()
        synth.engines.close()

    app = FastAPI(title="voxd", version="1.0", lifespan=lifespan)
    app.state.synth = synth
    app.state.sched = sched

    def auth(request: Request) -> None:
        if not cfg.token:
            return
        got = request.headers.get("authorization", "")
        if got != f"Bearer {cfg.token}":
            raise HTTPException(401, "token 不对")

    async def body_of(request: Request) -> SpeakRequest:
        try:
            raw = await request.json()
        except Exception:
            raise HTTPException(400, "body 必须是 JSON")
        if not isinstance(raw, dict):
            raise HTTPException(400, "body 必须是 JSON 对象")
        try:
            req = SpeakRequest.model_validate(raw)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, str(e))
        if len(req.text) > cfg.max_chars:
            raise HTTPException(413, f"文本 {len(req.text)} 字，超过上限 {cfg.max_chars}")
        return req

    # 试听包：直接挂目录，浏览器开 /audition/ 就能听（比双击 html 更稳）
    audition_dir = ROOT / "audition"
    if audition_dir.is_dir():
        app.mount("/audition", StaticFiles(directory=str(audition_dir), html=True), name="audition")

    # ── 试音台（一个静态页，不需要构建）──────────────────────────────
    @app.get("/")
    async def index():
        page = ROOT / "voxd" / "static" / "index.html"
        if not page.is_file():
            raise HTTPException(404, "试音台没装")
        return Response(content=page.read_bytes(), media_type="text/html; charset=utf-8")

    # ── 只读 ─────────────────────────────────────────────────────────
    @app.get("/v1/health")
    async def health():
        return {
            "ok": True,
            "uptime_s": int(time.time() - started),
            "engines": [e.model_dump() for e in synth.engines.info()],
            "queue": sched.depth(),
            "cache": synth.cache.stats(),
            "ffmpeg": bool(synth.ffmpeg),
        }

    @app.get("/v1/voices")
    async def voices():
        return {"voices": synth.voices.summary(), "default": cfg.default_voice}

    @app.get("/v1/engines")
    async def engines():
        return {"engines": [e.model_dump() for e in synth.engines.info()]}

    # ── 合成 ─────────────────────────────────────────────────────────
    @app.post("/v1/speak")
    async def speak(request: Request):
        auth(request)
        req = await body_of(request)
        try:
            job = await sched.submit(lambda j: synth.run(j, req), req.priority)
        except QueueFull as e:
            raise HTTPException(429, str(e))
        except (VoiceError, EngineError) as e:
            raise HTTPException(400, str(e))

        async def gen():
            try:
                while True:
                    f = await job.queue.get()
                    if f is None:
                        break
                    if f.get("type") == "seg":
                        f = {**f, "pcm": base64.b64encode(f["pcm"]).decode("ascii")}
                    yield _line(f)
            except asyncio.CancelledError:
                sched.cancel(job.id)
                raise
            finally:
                sched.cancel(job.id)

        return StreamingResponse(gen(), media_type="application/x-ndjson", headers={"X-Job-Id": job.id})

    @app.post("/v1/speak/file")
    async def speak_file(request: Request):
        auth(request)
        req = await body_of(request)
        try:
            job = await sched.submit(lambda j: synth.run(j, req), req.priority)
        except QueueFull as e:
            raise HTTPException(429, str(e))

        parts: list[bytes] = []
        meta: list[dict] = []
        err: str | None = None
        cached = False
        while True:
            f = await job.queue.get()
            if f is None:
                break
            t = f.get("type")
            if t == "start":
                cached = bool(f.get("cached"))
            elif t == "seg":
                parts.append(f["pcm"])
                meta.append({k: v for k, v in f.items() if k != "pcm"})
            elif t == "error":
                err = f.get("error") or "合成失败"
            elif t == "cancelled":
                err = err or "已取消"
        if err:
            raise HTTPException(503, err)

        pcm = b"".join(parts)
        try:
            out = codec.encode(pcm, req.sr, req.format, synth.ffmpeg)
        except RuntimeError as e:
            raise HTTPException(501, str(e))
        head = base64.b64encode(json.dumps(meta, ensure_ascii=False).encode("utf-8")).decode("ascii")
        return Response(
            content=out,
            media_type=MEDIA.get(req.format, "application/octet-stream"),
            headers={
                "X-Duration-Ms": str(codec.duration_ms(pcm, req.sr)),
                "X-Segments": head,
                "X-Job-Id": job.id,
                "X-Cached": "1" if cached else "0",
            },
        )

    @app.post("/v1/stop")
    async def stop(request: Request):
        auth(request)
        try:
            raw = await request.json()
        except Exception:
            raw = {}
        n = sched.cancel(raw.get("job_id") if isinstance(raw, dict) else None)
        return {"cancelled": n}

    return app


def main(argv: list[str] | None = None) -> None:
    import argparse

    import uvicorn

    ap = argparse.ArgumentParser("voxd")
    ap.add_argument("--config", default=str(ROOT / "config.toml"))
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--engine", default=None, help="覆盖 config 的 primary（临时切换引擎）")
    args = ap.parse_args(argv)
    cfg = Config(Path(args.config))
    if args.engine:
        cfg.primary = args.engine
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=args.port or cfg.port, log_level="info")
