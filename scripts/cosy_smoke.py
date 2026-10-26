"""CosyVoice2 冒烟：加载 → 用 instruct 合成 → 存 wav → 报 RTF。

    .venv-cosy\\Scripts\\python.exe scripts\\cosy_smoke.py [输出.wav]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "vendor" / "CosyVoice"))
sys.path.insert(0, str(ROOT / "vendor" / "CosyVoice" / "third_party" / "Matcha-TTS"))

MODEL = ROOT / "models" / "CosyVoice2-0.5B"
PROMPT = ROOT / "audition" / "vivian__neutral.wav"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "cosy_smoke.wav"

TEXT = "来了来了，我还以为你今天不来了呢。"
INSTRUCT = "用轻快、带笑意的语气说这句话<|endofprompt|>"


def main() -> None:
    import torch
    from cosyvoice.cli.cosyvoice import AutoModel

    print(f"torch={torch.__version__} cuda={torch.cuda.is_available()}", flush=True)
    t = time.perf_counter()
    m = AutoModel(model_dir=str(MODEL))
    print(f"load {time.perf_counter()-t:.1f}s sr={m.sample_rate}", flush=True)
    print("prompt:", PROMPT, "exists:", PROMPT.is_file(), flush=True)

    t = time.perf_counter()
    chunks = []
    for out in m.inference_instruct2(str(TEXT), INSTRUCT, str(PROMPT), stream=False, text_frontend=False):
        chunks.append(out["tts_speech"].squeeze(0).float().cpu().numpy())
    dt = time.perf_counter() - t
    if not chunks:
        print("!! 没有产出", flush=True)
        return
    y = np.concatenate(chunks)
    sr = m.sample_rate
    dur = len(y) / sr
    print(f"synth {dt:.2f}s audio={dur:.2f}s rtf={dt/dur:.2f}", flush=True)

    import wave

    with wave.open(str(OUT), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((np.clip(y, -1, 1) * 32767).astype("<i2").tobytes())
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()
