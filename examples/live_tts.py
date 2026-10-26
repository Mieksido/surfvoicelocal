"""直播用的流式配音示例：边合成边播，永远比播放快一步。

为什么需要它：
直播不能"一句合成完再开口"——那样每次都有 1 秒空档。正确做法是
**让合成跑在播放前面**（提前 20~30 秒备好），播放端只管按顺序取。

结构：

    脚本(每行一句) ──► 合成线程（2 个并发）──► 就绪队列（带时长）
                                                    │
                                          播放线程 ◄─┘ 按顺序写出/播放，落后就报警

缓冲低于阈值时自动切到"保底音色"（默认本地 gsv），保证不断流。

用法：

    # 用云端高质量音色为主，断流风险时退到本地
    python examples/live_tts.py --script lines.txt --voice age12-cloud \
        --fallback loli12-gsv --out live_out --lead 20

    # 从管道读（别的程序实时喂句子）
    type lines.txt | python examples/live_tts.py --stdin --out live_out

产出：`live_out/0001.wav, 0002.wav ...`，以及 `live_out/report.json`
（每句的耗时、缓冲水位、用的哪个音色）。
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

BASE = "http://127.0.0.1:8765"
SR = 24000


def synth(text: str, voice: str, base: str, timeout: float = 120.0) -> bytes:
    body = json.dumps({"text": text, "voice": voice}, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{base.rstrip('/')}/v1/speak/file",
        data=body,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def write_wav(path: Path, pcm: bytes) -> float:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm)
    return len(pcm) / 2 / SR


def main() -> None:
    ap = argparse.ArgumentParser(description="直播流式配音")
    ap.add_argument("--script", help="脚本文件，每行一句")
    ap.add_argument("--stdin", action="store_true", help="从标准输入逐行读")
    ap.add_argument("--voice", default="age12-cloud", help="主音色")
    ap.add_argument("--fallback", default="loli12-gsv", help="缓冲见底时的保底音色")
    ap.add_argument("--out", default="live_out", help="输出目录")
    ap.add_argument("--lead", type=float, default=20.0, help="提前备多少秒（默认 20）")
    ap.add_argument("--low", type=float, default=5.0, help="缓冲低于多少秒触发保底")
    ap.add_argument("--workers", type=int, default=2, help="并发合成线程数")
    ap.add_argument("--base", default=BASE)
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []
    if args.script:
        lines = [l.strip() for l in Path(args.script).read_text("utf-8").splitlines() if l.strip()]
    elif args.stdin:
        lines = [l.strip() for l in sys.stdin if l.strip()]
    if not lines:
        raise SystemExit("没有输入：用 --script 文件 或 --stdin 管道")

    ready: "queue.Queue[tuple[int, Path, float, str, float]]" = queue.Queue()
    # 待合成的句子也放队列里：多线程共用一个 idx 会重复合成同一句（踩过）
    todo: "queue.Queue[tuple[int, str]]" = queue.Queue()
    for i, text in enumerate(lines):
        todo.put((i, text))
    stop = threading.Event()
    report: list[dict] = []
    lock = threading.Lock()

    # 当前应使用的音色（缓冲低时会被切换）
    state = {"voice": args.voice, "switched": False}

    def producer() -> None:
        while not stop.is_set():
            if todo.empty():
                return
            # 缓冲已经够多就等一等（不去抢 GPU/网络）
            buffered = sum(item[2] for item in list(ready.queue))
            if buffered >= args.lead:
                time.sleep(0.2)
                continue
            try:
                idx, text = todo.get_nowait()
            except queue.Empty:
                return
            voice = state["voice"]
            t0 = time.perf_counter()
            try:
                pcm = synth(text, voice, args.base)
            except urllib.error.HTTPError as e:
                detail = e.read()[:200].decode("utf-8", "ignore")
                print(f"✗ 第 {idx+1} 句失败（{voice}）：{e.code} {detail}")
                # 主音色失败就退到保底，别断
                state["voice"] = args.fallback
                state["switched"] = True
                continue
            dt = time.perf_counter() - t0
            path = out / f"{idx+1:04d}.wav"
            dur = write_wav(path, pcm)
            ready.put((idx, path, dur, voice, dt))
            with lock:
                report.append({"i": idx + 1, "voice": voice, "synth_s": round(dt, 2), "audio_s": round(dur, 2)})
            print(f"合成 {idx+1:03d}  {voice:14s} {dt:4.2f}s → {dur:4.2f}s  {text[:18]}")

    def player() -> None:
        played = 0
        while played < len(lines) and not stop.is_set():
            try:
                idx, path, dur, voice, _ = ready.get(timeout=60)
            except queue.Empty:
                print("⚠️ 60 秒没有新音频，结束")
                return
            buffered = sum(item[2] for item in list(ready.queue))
            if buffered < args.low and not state["switched"]:
                state["voice"] = args.fallback
                state["switched"] = True
                print(f"⚠️ 缓冲只剩 {buffered:.1f}s，后续切到保底音色 {args.fallback}")
            # 这里用 sleep 模拟"播放"；接 OBS 时把 path 交给播放器即可
            time.sleep(dur)
            played += 1

    threads = [threading.Thread(target=producer, daemon=True) for _ in range(args.workers)]
    for t in threads:
        t.start()
    p = threading.Thread(target=player, daemon=True)
    p.start()

    try:
        p.join()
    except KeyboardInterrupt:
        stop.set()
        print("\n中断：已停止合成")
        try:
            urllib.request.urlopen(
                urllib.request.Request(f"{args.base}/v1/stop", data=b"{}", headers={"content-type": "application/json"}),
                timeout=5,
            ).read()
        except Exception:  # noqa: BLE001
            pass

    stop.set()
    (out / "report.json").write_text(
        json.dumps(
            {"voice": args.voice, "fallback": args.fallback, "switched": state["switched"], "items": report},
            ensure_ascii=False,
            indent=2,
        ),
        "utf-8",
    )
    print(f"\n完成：{len(report)} 句，输出在 {out}")


if __name__ == "__main__":
    main()
