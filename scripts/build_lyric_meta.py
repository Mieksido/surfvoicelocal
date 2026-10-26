"""给 SoulX-Singer 造一份"歌词 + 旋律"的元数据（以及一条同旋律的参考音）。

SoulX-Singer 的输入不是"一句话"，而是一份逐音节的乐谱式 JSON：

    text       每个字用空格分开，乐句边界放 <SP>
    phoneme    zh_ + 拼音 + 声调，例如 zh_ting1
    note_pitch MIDI 音高，0 = 休止
    note_type  1 乐句起始 / 2 普通 / 3 乐句收尾
    duration   每个音节的秒数（总和 = 总时长）
    f0         每 20ms 一帧的基频（Hz），50fps

这个脚本负责把"两句歌词"变成这样一份 JSON，并渲染一条同旋律的正弦参考音，
方便先用 /transcription_function 拿到模板，再替换成我们自己的词和调。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import soundfile as sf
from pypinyin import Style, pinyin

FPS = 50


def midi_to_hz(m: int) -> float:
    return 0.0 if m <= 0 else 440.0 * 2 ** ((m - 69) / 12)


def build(lyrics: list[list[str]], pitches: list[list[int]], durations: list[list[float]]):
    text_parts: list[str] = []
    phon_parts: list[str] = []
    note_pitch: list[int] = []
    note_type: list[int] = []
    dur: list[float] = []
    f0: list[float] = []

    for pi, (chars, notes) in enumerate(zip(lyrics, pitches)):
        # 乐句边界也要占一个"音节"：文本是 <SP>，音高 0、类型 1、给一点停顿
        text_parts.append("<SP>")
        phon_parts.append("<SP>")
        note_pitch.append(0)
        note_type.append(1)
        dur.append(0.2)
        f0.extend([0.0] * int(round(0.2 * FPS)))
        for ci, ch in enumerate(chars):
            py = pinyin(ch, style=Style.TONE3, neutral_tone_with_five=True)[0][0]
            text_parts.append(ch)
            phon_parts.append(f"zh_{py}")
            m = notes[ci]
            d = durations[pi][ci]
            note_pitch.append(m)
            note_type.append(1 if ci == 0 else (3 if ci == len(chars) - 1 else 2))
            dur.append(d)
            n_frames = int(round(d * FPS))
            if m <= 0:
                f0.extend([0.0] * n_frames)
                continue
            base = midi_to_hz(m)
            for k in range(n_frames):
                # 轻微颤音 + 起音滑音，避免听起来像正弦波
                vib = 1 + 0.012 * math.sin(2 * math.pi * 5.5 * k / FPS)
                glide = 1.0 if k > 2 else 0.96 + 0.04 * (k / 3)
                f0.append(round(base * vib * glide, 1))

    total = sum(dur)
    entry = {
        "index": "vocal_0_%d" % int(total * 1000),
        "language": "Mandarin",
        "time": [0, int(total * 1000)],
        "duration": " ".join(f"{d:.2f}" for d in dur),
        "text": " ".join(text_parts),
        "phoneme": " ".join(phon_parts),
        "note_pitch": " ".join(str(m) for m in note_pitch),
        "note_type": " ".join(str(t) for t in note_type),
        "f0": " ".join(f"{v:.1f}" for v in f0),
    }
    return [entry], total


def render_melody(entry: dict, out: Path, sr: int = 44100) -> float:
    f0 = [float(x) for x in entry["f0"].split()]
    t = np.arange(len(f0)) / FPS
    hz = np.array(f0)
    phase = np.cumsum(2 * np.pi * hz / FPS)
    sig = np.sin(phase) * 0.25
    # 简单的包络，避免咔哒声
    env = np.ones_like(sig)
    n = int(0.02 * FPS)
    env[:n] = np.linspace(0, 1, n)
    env[-n:] = np.linspace(1, 0, n)
    sig = sig * env
    sig = np.repeat(sig, int(sr / FPS))[: int(len(sig) * sr / FPS)]
    sf.write(str(out), sig.astype(np.float32), sr)
    return len(sig) / sr


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="logs/soulx/lyric_meta.json")
    ap.add_argument("--melody", default="logs/soulx/lyric_melody.wav")
    ap.add_argument("--transpose", type=int, default=0, help="整体升降调（半音）；age12 的说话基频约 350Hz，比原调高八度更自然")
    args = ap.parse_args()

    # 「听雨的声音 一滴滴清晰 / 你的呼吸像雨滴渗入我的爱里」
    lyrics = [list("听雨的声音一滴滴清晰"), list("你的呼吸像雨滴渗入我的爱里")]
    # 自拟旋律（C 大调，从小字一组的 a 起，整体下行再回到高处）
    pitches = [
        [62, 62, 60, 59, 57, 59, 59, 57, 57, 55],
        [59, 59, 57, 57, 55, 55, 57, 55, 53, 53, 55, 56, 57],
    ]
    durations = [
        [0.46, 0.42, 0.40, 0.44, 0.78, 0.44, 0.40, 0.40, 0.42, 0.72],
        [0.44, 0.40, 0.42, 0.40, 0.44, 0.40, 0.42, 0.40, 0.42, 0.40, 0.42, 0.44, 0.80],
    ]

    if args.transpose:
        pitches = [[p + args.transpose if p else 0 for p in row] for row in pitches]
    meta, total = build(lyrics, pitches, durations)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    dur = render_melody(meta[0], Path(args.melody))
    print(f"音节数 {len(meta[0]['note_pitch'].split())}  总时长 {total:.2f}s  参考音 {dur:.2f}s")
    print(f"元数据 -> {args.out}\n参考音 -> {args.melody}")
    print("text:", meta[0]["text"][:80])
    print("phoneme:", meta[0]["phoneme"][:80])


if __name__ == "__main__":
    main()
