"""生成一套「试听包」：固定语料 × 若干音色，落到 audition/，并生成一个可点的 index.html。

用途：换引擎/换音色时，人耳对比同一批句子。跑在服务上（不是直接调引擎），
顺带验证 HTTP 路由、标签、缓存。

    .venv\\Scripts\\python scripts/audition.py
    .venv\\Scripts\\python scripts/audition.py --voices huayan,vivian --base-url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import time
import urllib.request
import wave
from pathlib import Path

CORPUS = [
    ("neutral", "今天天气不错，下午要不要出去走走？"),
    ("warm", "[温柔]别急，慢慢说，我在听。"),
    ("happy", "[笑]你居然还记得这个，我都快忘了。"),
    ("annoyed", "[不耐烦]这句话你已经说第三遍了。"),
    ("sad", "[叹气]好吧，那就这样吧。"),
]

VOICES_DIR = Path(__file__).resolve().parent.parent / "voices"
DEFAULT_VOICES = ["huayan", "vivian", "serena", "laofu"]


def synth(base: str, text: str, voice: str) -> tuple[bytes, dict]:
    body = json.dumps({"text": text, "voice": voice, "format": "wav"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/v1/speak/file", data=body, headers={"content-type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read(), {
            "duration_ms": r.headers.get("X-Duration-Ms"),
            "cached": r.headers.get("X-Cached"),
        }


ENGINE_LABEL = {
    "piper": ("Piper 库存音色", "#7fbf7f", "0.2 秒出声，但没有情绪"),
    "qwen3": ("Qwen3 内置音色", "#7aa2f7", "离线生成，一句要十几秒"),
    "cosy": ("CosyVoice 克隆", "#e0a86a", "3~5 秒，支持情绪指令"),
}
VOICE_ORDER = {"piper": 0, "qwen3": 1, "cosy": 2}


def voice_meta(voices_dir: Path) -> dict[str, dict]:
    out = {}
    for d in sorted(voices_dir.iterdir()) if voices_dir.is_dir() else []:
        meta = d / "voice.json"
        if not meta.is_file():
            continue
        try:
            import json

            j = json.loads(meta.read_text("utf-8"))
        except Exception:  # noqa: BLE001
            continue
        out[d.name] = j
    return out



def _data_uri(base_dir: Path | None, name: str) -> str:
    """把 wav 内嵌成 data URI：页面变成单文件，任何预览器/双击打开都能放。"""
    if base_dir is None:
        return name
    try:
        raw = (Path(base_dir) / name).read_bytes()
    except OSError:
        return name
    return "data:audio/wav;base64," + base64.b64encode(raw).decode("ascii")


def page(rows: list[dict], voices: list[str], meta: dict[str, dict] | None = None,
         base_dir: Path | None = None) -> str:
    meta = meta or {}
    parts = [
        "<!doctype html><meta charset='utf-8'><title>voxd 试听包</title>",
        "<style>body{background:#14161a;color:#e6e8eb;font:15px/1.7 system-ui,'Microsoft YaHei';"
        "margin:0;padding:28px 4vw;max-width:900px}h1{font-size:20px}h2{font-size:16px;margin-top:34px;"
        "display:flex;align-items:center;gap:10px}"
        "div.clip{background:#1c1f25;border:1px solid #2b3038;border-radius:10px;padding:12px 14px;margin:10px 0}"
        "audio{width:100%;margin-top:8px}code{color:#7aa2f7}"
        ".badge{font-size:12px;padding:2px 9px;border-radius:999px;border:1px solid;font-weight:400}"
        ".desc{color:#8b93a1;font-size:13px;margin:-4px 0 10px}</style>",
        "<h1>voxd 试听包</h1>",
        "<p style='color:#8b93a1'>同一批语料（第 1 条是中性，后面带 <code>[标签]</code>）。"
        "重点听两件事：<b>是不是同一个人</b>、<b>情绪有没有真的变化</b>。</p>",
        "<p style='color:#8b93a1;font-size:13px'>"
        "<span class='badge' style='color:#7fbf7f;border-color:#7fbf7f'>Piper 库存音色</span> 当前默认，最快；"
        "<span class='badge' style='color:#7aa2f7;border-color:#7aa2f7'>Qwen3 内置音色</span> 离线，慢；"
        "<span class='badge' style='color:#e0a86a;border-color:#e0a86a'>CosyVoice 克隆</span> 带情绪，3~5 秒"
        "</p>",
    ]
    def sort_key(v):
        eng = (meta.get(v) or {}).get("engine") or "qwen3"
        return (VOICE_ORDER.get(eng, 9), v)

    for v in sorted(voices, key=sort_key):
        m = meta.get(v) or {}
        eng = m.get("engine") or "qwen3"
        label, color, note = ENGINE_LABEL.get(eng, (eng, "#8b93a1", ""))
        parts.append(
            f"<h2>{html.escape(v)}"
            f"<span class='badge' style='color:{color};border-color:{color}'>{label}</span></h2>"
            f"<div class='desc'>{html.escape(m.get('desc') or '')}"
            f"{' · ' + note if note else ''}</div>"
        )
        for row in rows:
            if row["voice"] != v:
                continue
            parts.append(
                "<div class='clip'>"
                f"<b>{row['tag']}</b> · {html.escape(row['text'])}<br>"
                f"<span style='color:#8b93a1;font-size:13px'>"
                f"{(str(row['ms']) + 'ms 生成 · ') if row.get('ms') else ''}"
                f"{row['dur']}s 音频{row['degraded']}</span>"
                f"<audio controls preload='metadata' src='{_data_uri(base_dir, row['file'])}'></audio>"
                "</div>"
            )
    return chr(10).join(parts)


def wav_ms(path: Path) -> int:
    try:
        with wave.open(str(path)) as w:
            return int(w.getnframes() / w.getframerate() * 1000)
    except Exception:  # noqa: BLE001
        return 0


def rebuild_index(out: Path) -> None:
    """扫描目录里已有的 wav，重建 index.html（跨多次运行合并）。"""
    rows = []
    for f in sorted(out.glob("*.wav")):
        voice, _, tag = f.stem.partition("__")
        text = next((t for tg, t in CORPUS if tg == tag), "")
        rows.append({"voice": voice, "tag": tag, "text": text, "file": f.name,
                     "ms": 0, "dur": round(wav_ms(f) / 1000, 2), "degraded": ""})
    voices = list(dict.fromkeys(r["voice"] for r in rows))
    (out / "index.html").write_text(page(rows, voices, voice_meta(VOICES_DIR), base_dir=out), "utf-8")
    print(f"{len(rows)} 条（{len(voices)} 个音色）→ {out / 'index.html'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8765")
    ap.add_argument("--voices", default=",".join(DEFAULT_VOICES))
    ap.add_argument("--out", default="audition")
    ap.add_argument("--rebuild-index", action="store_true", help="只从已有 wav 重建页面，不重新合成")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    voices = [v.strip() for v in args.voices.split(",") if v.strip()]
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.rebuild_index:
        out = Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        rebuild_index(out)
        return

    rows: list[dict] = []
    for voice in voices:
        for tag, text in CORPUS:
            t0 = time.perf_counter()
            try:
                data, meta = synth(base, text, voice)
            except Exception as e:  # noqa: BLE001
                print(f"FAIL {voice}/{tag}: {e}", flush=True)
                continue
            ms = int((time.perf_counter() - t0) * 1000)
            name = f"{voice}__{tag}.wav"
            (out / name).write_bytes(data)
            dur = round(int(meta.get("duration_ms") or 0) / 1000, 2)
            rows.append(
                {
                    "voice": voice,
                    "tag": tag,
                    "text": text,
                    "file": name,
                    "ms": ms,
                    "dur": dur,
                    "degraded": " · 缓存" if meta.get("cached") == "1" else "",
                }
            )
            print(f"{voice:10} {tag:9} {ms:6}ms  {dur:5}s  {text[:18]}", flush=True)

    (out / "index.html").write_text(page(rows, voices, voice_meta(VOICES_DIR), base_dir=out), "utf-8")
    print(f"\n{len(rows)} 条 → {out / 'index.html'}（浏览器直接打开）")


if __name__ == "__main__":
    main()
