"""把"哼唱录音"变成 SoulX-Singer 能吃的乐谱元数据。

为什么要这一步：
SoulX 的输入不是音频，而是一份**逐音节的乐谱**（音高/时值/音素/f0）。
所以要么有 MIDI/简谱，要么从一段录音里把旋律"抽"出来。

这个脚本做四件事：
1. pyworld harvest 提基频（比自相关稳）
2. **八度纠错**：低八度的帧 +12，避免旋律忽高忽低
3. 平滑 + 量化到半音 + 游程编码成"音符 + 时值"
4. 把音符序列拉伸/合并到歌词的音节数，输出乐谱 JSON

用法：
    python scripts/hum_to_meta.py --wav logs/hum/hum.wav --lyric "听雨的声音一滴滴清晰" \
        --lyric2 "你的呼吸像雨滴渗入我的爱里" --out logs/hum/meta.json
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np
import pyworld as pw
import soundfile as sf
from pypinyin import Style, pinyin
from scipy.signal import medfilt

FPS = 50


def hum_notes(wav: Path) -> tuple[list[int], list[float], np.ndarray]:
    x, sr = sf.read(str(wav), dtype="float64")
    if x.ndim > 1:
        x = x.mean(axis=1)
    f0, t = pw.harvest(x, sr, f0_floor=80, f0_ceil=800, frame_period=1000 / FPS)
    semi = np.array([12 * math.log2(f / 440) + 69 if f > 0 else 0.0 for f in f0])

    # 八度纠错：比局部中位数低 7 个半音以上的，认为掉了一个八度
    voiced = semi > 0
    idx = np.where(voiced)[0]
    if len(idx) > 10:
        med = medfilt(semi[idx], min(101, len(idx) // 2 * 2 + 1))
        for j, i in enumerate(idx):
            while semi[i] > 0 and semi[i] < med[j] - 7:
                semi[i] += 12
    if voiced.sum() > 9:
        semi[voiced] = medfilt(semi[voiced], 9)

    # 量化 + 游程
    notes: list[int] = []
    durs: list[float] = []
    cur = 0
    cnt = 0
    for v in semi:
        q = int(round(v)) if v > 0 else 0
        if q == 0:
            if cnt > 0:
                notes.append(cur)
                durs.append(cnt / FPS)
                cnt = 0
            cur = 0
            continue
        if q != cur:
            if cnt > 0:
                notes.append(cur)
                durs.append(cnt / FPS)
            cur = q
            cnt = 1
        else:
            cnt += 1
    if cnt > 0:
        notes.append(cur)
        durs.append(cnt / FPS)

    # 合并过短的音符（<0.15s），保留长音
    mn, md = [], []
    for n, d in zip(notes, durs):
        if d < 0.15 and mn:
            md[-1] += d
        else:
            mn.append(n)
            md.append(d)
    return mn, md, semi


def fit_to_syllables(notes: list[int], durs: list[float], n: int) -> tuple[list[int], list[float]]:
    """把音符序列压/抻到正好 n 个音节。"""
    if not notes:
        return [60] * n, [0.45] * n
    if len(notes) > n:
        # 均匀挑 n 个位置，被跳过的音符并进相邻时值
        keep = [notes[int(round(i * (len(notes) - 1) / max(1, n - 1)))] for i in range(n)]
        total = sum(durs)
        return keep, [total / n] * n
    # 音符比音节少：把最长的几个拆开
    while len(notes) < n:
        k = int(np.argmax(durs))
        d = durs[k]
        notes = notes[: k + 1] + [notes[k]] + notes[k + 1 :]
        durs = durs[:k] + [d / 2, d / 2] + durs[k + 1 :]
    return notes, durs


def build(lyrics: list[list[str]], notes: list[int], durs: list[float]) -> tuple[list[dict], float]:
    text_parts, phon_parts = [], []
    note_pitch, note_type, dur, f0 = [], [], [], []
    i = 0
    for pi, chars in enumerate(lyrics):
        text_parts.append("<SP>")
        phon_parts.append("<SP>")
        note_pitch.append(0)
        note_type.append(1)
        dur.append(0.18)
        f0.extend([0.0] * int(round(0.18 * FPS)))
        for ci, ch in enumerate(chars):
            py = pinyin(ch, style=Style.TONE3, neutral_tone_with_five=True)[0][0]
            text_parts.append(ch)
            phon_parts.append(f"zh_{py}")
            m = notes[i] if i < len(notes) else 60
            d = durs[i] if i < len(durs) else 0.45
            i += 1
            note_pitch.append(m)
            note_type.append(1 if ci == 0 else (3 if ci == len(chars) - 1 else 2))
            dur.append(round(d, 3))
            nf = int(round(d * FPS))
            base = 440 * 2 ** ((m - 69) / 12)
            for k in range(nf):
                vib = 1 + 0.01 * math.sin(2 * math.pi * 5.5 * k / FPS)
                f0.append(round(base * vib, 1))
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", required=True)
    ap.add_argument("--lyric", required=True, help="第一句（不带空格）")
    ap.add_argument("--lyric2", default="", help="第二句")
    ap.add_argument("--transpose", type=int, default=0)
    ap.add_argument("--seconds", type=float, default=0, help="只取录音前 N 秒的旋律（0=全部）")
    ap.add_argument("--out", default="logs/hum/meta.json")
    args = ap.parse_args()

    notes, durs, _ = hum_notes(Path(args.wav))
    if args.seconds > 0:
        acc, cut = 0.0, 0
        for d in durs:
            acc += d
            cut += 1
            if acc >= args.seconds:
                break
        notes, durs = notes[:cut], durs[:cut]
    print(f"从录音里抽出 {len(notes)} 个音符，总长 {sum(durs):.1f}s，音域 {min(notes)}~{max(notes)} (MIDI)")

    lyrics = [list(args.lyric)] + ([list(args.lyric2)] if args.lyric2 else [])
    n = sum(len(c) for c in lyrics)
    notes, durs = fit_to_syllables(notes, durs, n)
    if args.transpose:
        notes = [m + args.transpose for m in notes]
    meta, total = build(lyrics, notes, durs)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")
    print(f"音节 {n} 个，总时长 {total:.1f}s → {args.out}")


if __name__ == "__main__":
    main()
