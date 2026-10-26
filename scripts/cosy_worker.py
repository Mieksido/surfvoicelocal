"""CosyVoice2-0.5B 独立 worker：跑在**自己的 venv** 里，只负责「文本 + 情绪指令 → wav」。

为什么要独立进程：
· CosyVoice 钉 torch/numpy/transformers 的版本，和 voxd 的 Qwen3 环境互相打架；
· 跑成 HTTP 服务后，voxd 只需要一个 `remote` 引擎适配器，协议与宿主完全不变。

启动（用 .venv-cosy）：
    .venv-cosy\\Scripts\\python.exe scripts\\cosy_worker.py --port 8766

接口：
    GET  /health
    POST /say   {text, instruct?, prompt?, speed?, stream?} → audio/wav
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
REPO = ROOT / "vendor" / "CosyVoice"
MODEL_DIR = ROOT / "models" / "CosyVoice2-0.5B"

sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "third_party" / "Matcha-TTS"))

class SayReq(BaseModel):
    """请求体。必须放在模块级：main() 里的局部类在 future annotations 下解析不到。"""

    text: str
    instruct: str = ""
    prompt: str = ""
    speed: float = 1.0
    spk: str = ""
    stream: bool = False


_lock = threading.Lock()
_model = None
_load_error: str | None = None
_load_ms = 0
_speakers: list[str] = []

VOICES_DIR = ROOT / "voices"


def register_speakers() -> list[str]:
    """把 voices/* 里 engine=cosy 的音色注册成 zero-shot 说话人。

    注册一次，之后每次合成省掉约 0.65s 的特征提取（whisper mel + ONNX tokenizer + campplus）。
    """
    if _model is None:
        return []
    done = []
    for d in sorted(VOICES_DIR.iterdir()):
        meta = d / "voice.json"
        if not meta.is_file():
            continue
        try:
            j = json.loads(meta.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if j.get("engine") != "cosy":
            continue
        ref = d / (j.get("ref_audio") or "ref.wav")
        if not ref.is_file():
            continue
        try:
            if _model.add_zero_shot_spk(j.get("ref_text", ""), str(ref), d.name):
                done.append(d.name)
        except Exception as e:  # noqa: BLE001
            print(f"[cosy] 注册音色 {d.name} 失败：{e}", flush=True)
    return done


def load_model():
    global _model, _load_error, _load_ms
    if _model is not None or _load_error:
        return
    t = time.perf_counter()
    try:
        from cosyvoice.cli.cosyvoice import AutoModel

        _model = AutoModel(model_dir=str(MODEL_DIR))
        _load_ms = int((time.perf_counter() - t) * 1000)
        t2 = time.perf_counter()
        _speakers[:] = register_speakers()
        print(f"[cosy] 注册说话人 {_speakers} 用时 {time.perf_counter()-t2:.1f}s", flush=True)
        # 预热一次：把 CUDA 首次分配/kernel 编译的成本从用户的第一次请求里挪走
        t3 = time.perf_counter()
        try:
            synth("你好呀。", "用自然的语气说这句话<|endofprompt|>", str(VOICES_DIR / "coco" / "ref.wav"))
            print(f"[cosy] 预热完成 {time.perf_counter()-t3:.1f}s", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[cosy] 预热失败（不影响服务）：{e}", flush=True)
    except Exception as e:  # noqa: BLE001
        import traceback

        _load_error = f"{type(e).__name__}: {e}"
        traceback.print_exc()


def wav_bytes(x: np.ndarray, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype("<i2").tobytes())
    return buf.getvalue()


def synth(text: str, instruct: str = "", prompt: str = "", speed: float = 1.0, spk: str = "") -> tuple[bytes, int]:
    load_model()
    if _model is None:
        raise RuntimeError(f"模型没加载成功：{_load_error}")
    sr = int(getattr(_model, "sample_rate", 24000) or 24000)
    parts = []
    with _lock:
        if instruct:
            gen = _model.inference_instruct2(
                text, instruct, prompt, zero_shot_spk_id=spk,
                stream=False, speed=speed, text_frontend=False,
            )
        else:
            gen = _model.inference_zero_shot(
                text, "", prompt, zero_shot_spk_id=spk, stream=False, speed=speed, text_frontend=False
            )
        for out in gen:
            parts.append(out["tts_speech"].squeeze(0).float().cpu().numpy())
    if not parts:
        raise RuntimeError("没有产出音频")
    return wav_bytes(np.concatenate(parts), sr), sr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8766)
    ap.add_argument("--preload", action="store_true")
    args = ap.parse_args()

    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse, Response
    from pydantic import BaseModel
    import uvicorn

    app = FastAPI(title="cosy-worker")

    @app.get("/health")
    async def health():
        return JSONResponse(
            {
                "ok": _model is not None and not _load_error,
                "loaded": _model is not None,
                "error": _load_error,
                "load_ms": _load_ms,
                "model": str(MODEL_DIR),
                "speakers": _speakers,
            }
        )

    @app.post("/say")
    async def say(req: SayReq):
        if not req.text.strip():
            raise HTTPException(400, "text 为空")
        t = time.perf_counter()
        try:
            data, sr = await __import__("asyncio").to_thread(
                synth, req.text, req.instruct, req.prompt, req.speed, req.spk
            )
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, f"{type(e).__name__}: {e}")
        ms = int((time.perf_counter() - t) * 1000)
        return Response(
            content=data,
            media_type="audio/wav",
            headers={"X-Sr": str(sr), "X-Elapsed-Ms": str(ms)},
        )

    if args.preload:
        threading.Thread(target=load_model, daemon=True).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
