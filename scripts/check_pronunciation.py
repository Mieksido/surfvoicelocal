"""发音体检：合成一组句子，再用 ASR 回读，逐字比对。

为什么需要它：耳朵能听出"听→qing""凉→lia""吗拖长"，但没法量化。
这个脚本把"听得对不对"变成可比较的数字，训练前后各跑一次就知道有没有变好。

用法：
    ./.venv-gsv/Scripts/python.exe scripts/check_pronunciation.py --port 9880
    ... --port 9881 --tag v3

注意：
- 它是**客观下限**，不是全部。ASR 听对了不代表好听，但听错了基本就是有问题。
- 同音字（她/他、的/得）会被算成"错"，看输出时人工排除。
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REF = "D:/deepseekwork/voice/voicepack/age12/01.wav"
REF_TEXT = "今天天气不错，下午要不要出去走走？"

# 这些句子覆盖用户点名过的坑：t/q、-iang、-ing、轻声虚词
SENTS = [
    "听见了吗？这是我用语音说的。",
    "你听，外面在下雨。",
    "清晨的风有点凉。",
    "她的声音很轻。",
    "我们停下来拍张照。",
    "请你亲口告诉我。",
    "风有点凉。",
    "天快亮了。",
    "你的声音很好听。",
    "这样好吗？",
    "我们走吧。",
    "现在已经很晚了。",
]

STRIP = str.maketrans("", "", "？，。！？、；：")


def norm(s: str) -> str:
    return re.sub(r"\s+", "", s).translate(STRIP)


def tts(port: int, text: str, out: Path, seed: int = 1) -> None:
    body = {
        "text": text,
        "text_lang": "zh",
        "ref_audio_path": REF,
        "prompt_text": REF_TEXT,
        "prompt_lang": "zh",
        "media_type": "wav",
        # 和冻结音色一致的采样参数
        "temperature": 1.0,
        "top_k": 15,
        "top_p": 1.0,
        "seed": seed,
    }
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/tts",
        data=json.dumps(body, ensure_ascii=False).encode(),
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        out.write_bytes(r.read())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9880)
    ap.add_argument("--tag", default="run")
    ap.add_argument("--seed", type=int, default=1)
    args = ap.parse_args()

    out_dir = ROOT / "logs" / "pron" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    from funasr import AutoModel

    asr = AutoModel(model="paraformer-zh", disable_update=True, hub="ms")

    ok = 0
    rows = []
    for i, text in enumerate(SENTS, 1):
        wav = out_dir / f"{i:02d}.wav"
        tts(args.port, text, wav, args.seed)
        got = norm(asr.generate(input=str(wav), batch_size_s=300)[0]["text"])
        want = norm(text)
        hit = got == want
        ok += hit
        rows.append({"text": text, "want": want, "asr": got, "ok": hit})
        print(f"{'✓' if hit else '✗'} {text}  →  {got}")

    print(f"\n{args.tag}: {ok}/{len(SENTS)} 句完全正确")
    (out_dir / "result.json").write_text(
        json.dumps({"port": args.port, "score": ok, "total": len(SENTS), "rows": rows}, ensure_ascii=False, indent=2),
        "utf-8",
    )
    print(f"明细：{out_dir / 'result.json'}")


if __name__ == "__main__":
    main()
