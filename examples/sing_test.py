"""唱歌可行性测试：用 Seed-VC 的 f0 模型做"音色转换"，看能不能把旋律保住。

为什么是这个方案：
RVC / So-VITS-SVC 都要 fairseq，而 fairseq 在 Python 3.13 + Windows 上基本装不上
（我们这台就是 3.13）。Seed-VC 的 f0 变体是**带音高条件**的变声模型，
天生就是给唱歌/哼唱准备的：保留音高曲线，只换音色。

测试做法：
1. 造一段"有旋律"的输入（把一句话按音高分段变调，模拟唱出来的起伏）
2. 用 f0 模型转成 age12 的音色
3. 逐段量输出基频，看它是否跟着输入的旋律走

结论看 correlation 和逐段误差（半音）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

ROOT = Path(__file__).resolve().parent.parent
VC_PKG = ROOT / ".venv-vc" / "Lib" / "site-packages" / "seed_vc"
sys.path.insert(0, str(VC_PKG.resolve()))

# Seed-VC 的流式分支会用 pydub 编 mp3，需要 ffmpeg 在 PATH 里
import os

_ff = ROOT / "tools" / "bin"
if _ff.is_dir():
    os.environ["PATH"] = str(_ff) + os.pathsep + os.environ.get("PATH", "")
try:
    from pydub import AudioSegment

    AudioSegment.converter = str(_ff / "ffmpeg.exe")
except Exception:  # noqa: BLE001
    pass


def estimate_f0(x: np.ndarray, sr: int, frame_ms: float = 30.0, hop_ms: float = 10.0) -> np.ndarray:
    """非常朴素的基频跟踪：自相关峰值。够用来比较两条曲线。"""
    win, hop = int(sr * frame_ms / 1000), int(sr * hop_ms / 1000)
    out = []
    for i in range(0, max(1, len(x) - win), hop):
        seg = x[i : i + win]
        if np.sqrt((seg**2).mean()) < 0.01:
            out.append(0.0)
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[win - 1 :]
        lo, hi = int(sr / 600), int(sr / 80)
        if hi >= len(ac):
            out.append(0.0)
            continue
        lag = lo + int(np.argmax(ac[lo:hi]))
        out.append(float(sr / lag) if ac[lag] > 0.3 * ac[0] else 0.0)
    return np.array(out)


def make_melody(src: np.ndarray, sr: int, notes: list[int], note_s: float = 0.45) -> np.ndarray:
    """把一段话按音符列表分段变调，模拟"唱"出来的音高起伏（保持时长不变）。"""
    out = []
    for k, semi in enumerate(notes):
        a = int(k * note_s * sr)
        b = min(len(src), int((k + 1) * note_s * sr))
        if a >= len(src):
            break
        seg = src[a:b]
        if len(seg) < 64:
            continue
        factor = 2 ** (semi / 12)
        n = len(seg)
        idx = np.linspace(0, n - 1, n)
        stretched = np.interp(idx / factor, np.arange(n), seg)  # 变调（同时变时长）
        # 长度拉回原来的 n，保持节奏
        out.append(np.interp(np.linspace(0, len(stretched) - 1, n), np.arange(len(stretched)), stretched))
    return np.concatenate(out) if out else src


def main() -> None:
    from types import SimpleNamespace

    from seed_vc import inference_v2 as iv2
    from seed_vc.api import get_audio_numpy
    from seed_vc.Models.audio import AudioData

    def load(p: str) -> AudioData:
        a, sr = sf.read(p, dtype="float32", always_2d=True)
        a = a.mean(axis=1)
        return AudioData(
            samples=torch.from_numpy(a), mel_chunks=None, duration=len(a) / sr,
            samples_count=len(a), sample_rate=sr, metadata={},
        )

    # 1) 造旋律输入：用我们自己的年龄 12 参考音频当"人声干声"
    src_path = ROOT / "voicepack" / "age12" / "all.wav"
    src, sr = sf.read(str(src_path), dtype="float32", always_2d=True)
    src = src.mean(axis=1)
    # 小星星前两句的音程
    notes = [0, 0, 7, 7, 9, 9, 7, 5, 5, 4, 4, 2, 2, 0]
    melodic = make_melody(src, sr, notes, note_s=0.45)
    sf.write(str(ROOT / "logs" / "sing_in.wav"), melodic, sr)

    # 2) 用 f0 模型转音色（目标 = age12）
    args = SimpleNamespace(
        diffusion_steps=10, length_adjust=1.0, intelligibility_cfg_rate=0.7,
        similarity_cfg_rate=0.9, top_p=0.9, temperature=1.0, repetition_penalty=1.0,
        convert_style=False, anonymization_only=False, compile=False,
        ar_checkpoint_path=None, cfm_checkpoint_path=None,
    )
    if iv2.vc_wrapper_v2 is None:
        iv2.vc_wrapper_v2 = iv2.load_v2_models(args)

    source = load(str(ROOT / "logs" / "sing_in.wav"))
    target = load(str(ROOT / "voicepack" / "age12" / "01.wav"))

    out = None
    for item in iv2.vc_wrapper_v2.convert_voice_with_streaming_arrays(
        source_wave=get_audio_numpy(source), target_wave=get_audio_numpy(target),
        source_sr=int(source.sample_rate), target_sr=int(target.sample_rate),
        diffusion_steps=10, length_adjust=1.0, intelligebility_cfg_rate=0.7, similarity_cfg_rate=0.9,
        top_p=0.9, temperature=1.0, repetition_penalty=1.0, convert_style=False, anonymization_only=False,
        device=next(iv2.vc_wrapper_v2.parameters()).device, dtype=torch.float32, stream_output=True,
    ):
        out = item
    sr_out, audio = out[1]
    audio = np.asarray(audio).squeeze()
    sf.write(str(ROOT / "logs" / "sing_out.wav"), audio, int(sr_out))

    # 3) 比较两条音高曲线
    n = min(len(melodic), len(audio))
    f_in = estimate_f0(melodic[:n], sr)
    # 转出来是 44.1k，先重采样到同一采样率再比
    audio24 = np.interp(np.linspace(0, len(audio) - 1, int(len(audio) * sr / sr_out)), np.arange(len(audio)), audio)
    f_out = estimate_f0(audio24[:n], sr)

    m = (f_in > 60) & (f_out > 60)
    if m.sum() < 10:
        print("几乎没测到音高，转换可能失败")
        return
    semi_in = 12 * np.log2(f_in[m])
    semi_out = 12 * np.log2(f_out[m])
    corr = float(np.corrcoef(semi_in, semi_out)[0, 1])
    err = float(np.abs(semi_in - semi_out).mean())
    print(f"有效帧 {int(m.sum())}")
    print(f"音高曲线相关: {corr:.3f}  平均偏差: {err:.2f} 半音")
    print(f"输入中位 F0 {np.median(f_in[m]):.0f}Hz → 输出 {np.median(f_out[m]):.0f}Hz")
    print(f"产出: logs/sing_in.wav（旋律输入）, logs/sing_out.wav（转换后）")
    print("判定:", "✅ 旋律保住了（可以拿来唱）" if corr > 0.8 and err < 3 else "⚠️ 旋律跟随一般")


if __name__ == "__main__":
    main()
