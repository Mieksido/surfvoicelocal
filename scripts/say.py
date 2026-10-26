"""命令行配音：把文本合成成 wav，走本地或云端。

用途：
- 做视频的旁白/台词，一条条生成，文件名带序号
- 快速 A/B 对比本地和云端的音色
- 给直播调试素材

例子：
    # 一句话，云端（质量最好）
    ./.venv/Scripts/python.exe scripts/say.py "听见了吗？这是我用语音说的。" --voice age12-cloud

    # 本地（免费、快）
    ./.venv/Scripts/python.exe scripts/say.py "来了来了。" --voice loli12-gsv

    # 整篇台词，按行切成多条，输出到 out/ 目录
    ./.venv/Scripts/python.exe scripts/say.py --batch lines.txt --voice age12-cloud --out out

输出：wav（24kHz 16bit 单声道），默认写到当前目录。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASE = "http://127.0.0.1:8765"


def synth(text: str, voice: str, base: str, timeout: float) -> bytes:
    """调 voxd 的 /v1/speak/file，返回 PCM（无 wav 头）。"""
    body = json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/speak/file",
        data=body,
        headers={"content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "ignore")
        raise SystemExit(f"合成失败 {e.code}: {detail}") from e


def write_wav(path: Path, pcm: bytes, sr: int = 24000) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return len(pcm) / 2 / sr


def main() -> None:
    ap = argparse.ArgumentParser(description="配音：文本 → wav")
    ap.add_argument("text", nargs="?", help="要合成的文本")
    ap.add_argument("--batch", help="批量：每行一句，生成多条 wav")
    ap.add_argument("--voice", default="age12-cloud", help="音色 id（默认 age12-cloud）")
    ap.add_argument("--out", default=".", help="输出文件或目录")
    ap.add_argument("--base", default=DEFAULT_BASE, help="voxd 地址")
    ap.add_argument("--sr", type=int, default=24000)
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    out = Path(args.out)

    if args.batch:
        lines = [l.strip() for l in Path(args.batch).read_text("utf-8").splitlines() if l.strip()]
        if not lines:
            raise SystemExit("批量文件是空的")
        total = 0.0
        for i, text in enumerate(lines, 1):
            pcm = synth(text, args.voice, args.base, args.timeout)
            f = out / f"{i:03d}.wav"
            dur = write_wav(f, pcm, args.sr)
            total += dur
            print(f"{i:03d} {dur:5.2f}s  {text[:24]}  → {f}")
        print(f"\n共 {len(lines)} 条，{total:.1f} 秒音频")
        return

    if not args.text:
        ap.print_help()
        sys.exit(2)

    pcm = synth(args.text, args.voice, args.base, args.timeout)
    f = Path(args.out)
    if f.is_dir() or str(args.out).endswith(("/", "\\")):
        f = f / "say.wav"
    dur = write_wav(f, pcm, args.sr)
    print(f"{dur:.2f}s  → {f}")


if __name__ == "__main__":
    main()
