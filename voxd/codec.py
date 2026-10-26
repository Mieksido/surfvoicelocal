"""音频字节的搬运：float↔pcm16、拼接、淡入淡出、封装 wav、必要时调 ffmpeg。

约定：内部一律 **s16le 单声道裸 PCM**，只有出口才封装。
这样缓存、拼接、切片、流式全部是同一份字节，不用为每种格式写一遍。
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SAMPLE_WIDTH = 2


def to_pcm16(x: np.ndarray) -> bytes:
    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if a.size == 0:
        return b""
    peak = float(np.max(np.abs(a)))
    if peak > 1.0:  # 防削波，而不是让 int16 回绕成爆音
        a = a / peak
    return (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def from_pcm16(b: bytes) -> np.ndarray:
    if not b:
        return np.zeros(0, dtype=np.float32)
    return np.frombuffer(b, dtype="<i2").astype(np.float32) / 32767.0


def silence(ms: int, sr: int) -> bytes:
    n = int(sr * max(0, ms) / 1000)
    return b"\x00\x00" * n


def duration_ms(pcm: bytes, sr: int) -> int:
    return int(len(pcm) / SAMPLE_WIDTH / max(1, sr) * 1000)


def _fade(pcm: bytes, sr: int, ms: int = 12) -> bytes:
    """每小句首尾各加一个短斜坡，拼接处才不会「咔」。"""
    n = int(sr * ms / 1000)
    a = from_pcm16(pcm)
    if a.size < 2 * n or n <= 0:
        return pcm
    ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
    a[:n] *= ramp
    a[-n:] *= ramp[::-1]
    return to_pcm16(a)


def concat(chunks: list[bytes], sr: int, pauses: list[int] | None = None) -> bytes:
    out = bytearray()
    for i, c in enumerate(chunks):
        if i and pauses:
            out += silence(pauses[i - 1], sr)
        out += _fade(c, sr)
    return bytes(out)


def resample(pcm: bytes, sr_from: int, sr_to: int) -> bytes:
    if sr_from == sr_to or not pcm:
        return pcm
    a = from_pcm16(pcm)
    n = int(len(a) * sr_to / sr_from)
    if n <= 0:
        return b""
    x = np.linspace(0.0, len(a) - 1, n, dtype=np.float64)
    return to_pcm16(np.interp(x, np.arange(len(a)), a).astype(np.float32))


def apply_rate(pcm: bytes, sr: int, rate: float) -> bytes:
    """只在不支持 instruct 的引擎上兜底用。会同时改变音高——所以幅度限制在 ±8%。"""
    rate = max(0.92, min(1.08, float(rate)))
    if abs(rate - 1.0) < 1e-3 or not pcm:
        return pcm
    return resample(pcm, sr, int(sr * rate))


def wav_bytes(pcm: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()


def find_ffmpeg(explicit: str = "") -> str | None:
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("ffmpeg")


def encode(pcm: bytes, sr: int, fmt: str, ffmpeg: str | None = None) -> bytes:
    """出口封装。pcm/wav 不需要外部程序；ogg/mp3 需要 ffmpeg。"""
    if fmt == "pcm":
        return pcm
    if fmt == "wav":
        return wav_bytes(pcm, sr)
    if not ffmpeg:
        raise RuntimeError(f"输出 {fmt} 需要 ffmpeg：装一个或改用 format=wav")
    with tempfile.TemporaryDirectory(prefix="voxd-") as d:
        src = Path(d) / "in.wav"
        dst = Path(d) / f"out.{fmt}"
        src.write_bytes(wav_bytes(pcm, sr))
        codec = ["-c:a", "libopus", "-b:a", "48k"] if fmt == "ogg" else ["-c:a", "libmp3lame", "-q:a", "4"]
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), *codec, str(dst)],
            capture_output=True,
        )
        if proc.returncode != 0 or not dst.is_file():
            raise RuntimeError(f"ffmpeg 转 {fmt} 失败：{proc.stderr.decode('utf-8', 'replace')[:200]}")
        return dst.read_bytes()
