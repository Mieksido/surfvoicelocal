"""唱歌变声：把一首歌的人声换成目标音色（age12）。

原理：不是让 TTS 学会唱歌，而是**保留原唱的音高曲线，只替换音色**。
用的是 Seed-VC 的 f0 变体（44.1kHz，带音高条件），实测：
音高曲线相关 0.93、平均偏差 0.61 半音、时长不变。

前提：你要有一段**人声干声**（去掉伴奏的 vocal）。整首歌直接丢进去，
伴奏会被当成"内容"一起转换，效果会糊。

用法：
    # 最基本
    python scripts/sing.py --source vocal.wav --target voicepack/age12/01.wav --out out.wav

    # 调音高（半音）：女声唱男声的歌可以 +12 再转
    python scripts/sing.py --source vocal.wav --out out.wav --semi-tone 12

    # 更高质量（慢一倍）
    python scripts/sing.py --source vocal.wav --out out.wav --steps 30

说明：
- 依赖 `vendor/seed-vc/`（那套脚本已打好 Windows 补丁）
- 第一次跑会下 whisper-small / bigvgan-44k（约 1GB），之后走缓存
- 显存占用约 3~4G；和 voxd 同时跑要留意
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVC = ROOT / "vendor" / "seed-vc"
PY = ROOT / ".venv-vc" / "Scripts" / "python.exe"
CKPT = ROOT / "models" / "Seed-VC" / "DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema.pth"
CFG = "configs/presets/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml"


def main() -> None:
    ap = argparse.ArgumentParser(description="歌声变声（保留旋律，换音色）")
    ap.add_argument("--source", required=True, help="源人声（干声 wav）")
    ap.add_argument("--target", default=str(ROOT / "voicepack" / "age12" / "01.wav"), help="目标音色参考音频")
    ap.add_argument("--out", required=True, help="输出 wav 路径")
    ap.add_argument("--steps", type=int, default=10, help="扩散步数（10 快 / 30 质量好）")
    ap.add_argument("--semi-tone", type=int, default=0, help="整体升降调（半音）")
    ap.add_argument("--f0", default="True", help="跟随源音高（唱歌必须 True）")
    args = ap.parse_args()

    if not CKPT.is_file():
        raise SystemExit(f"缺少模型：{CKPT}")
    src = Path(args.source)
    if not src.is_file():
        raise SystemExit(f"源文件不存在：{src}")

    out_path = Path(args.out).resolve()
    tmp = ROOT / "logs" / "sing_tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True, exist_ok=True)

    cmd = [
        str(PY), "inference.py",
        "--source", str(src.resolve()),
        "--target", str(Path(args.target).resolve()),
        "--output", str(tmp),
        "--checkpoint", str(CKPT),
        "--config", CFG,
        "--f0-condition", args.f0,
        "--diffusion-steps", str(args.steps),
        "--semi-tone-shift", str(args.semi_tone),
        "--fp16", "False",
    ]
    print("运行：", " ".join(cmd[:6]), "...")
    p = subprocess.run(cmd, cwd=str(SVC))
    if p.returncode != 0:
        raise SystemExit(f"转换失败，退出码 {p.returncode}")

    made = list(tmp.glob("*.wav"))
    if not made:
        raise SystemExit("没有产出音频")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(made[0]), str(out_path))
    print(f"完成：{out_path}")


if __name__ == "__main__":
    main()
