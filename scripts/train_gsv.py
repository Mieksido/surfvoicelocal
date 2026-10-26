"""GPT-SoVITS v2 微调驱动：复刻 age12 的声音。

用法（在 voice/ 目录下）：
    ./.venv-gsv/Scripts/python.exe scripts/train_gsv.py --exp age12 --data dataset/age12 \
        --s2-epochs 8 --s1-epochs 10 --batch 4

它做的事等价于 WebUI 里点的那 5 个按钮：
  1-get-text → 2-get-hubert-wav32k → 3-get-semantic → s2_train(SoVITS) → s1_train(GPT)

产物：
  logs/<exp>/logs_s2_v2/*.pth            SoVITS 权重
  logs/<exp>/logs_s1_v2/*.ckpt           GPT 权重
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GSV = ROOT / "vendor" / "GPT-SoVITS" / "code"
PY = ROOT / ".venv-gsv" / "Scripts" / "python.exe"
PRE = GSV / "GPT_SoVITS" / "pretrained_models"


def run(cmd: list[str], env: dict[str, str], label: str) -> None:
    print(f"\n=== {label} ===", flush=True)
    print(" ".join(cmd), flush=True)
    p = subprocess.run(cmd, cwd=str(GSV), env=env)
    if p.returncode != 0:
        raise SystemExit(f"{label} 失败，退出码 {p.returncode}")
    print(f"--- {label} 完成 ---", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exp", default="age12")
    ap.add_argument("--data", default="dataset/age12")
    ap.add_argument("--s2-epochs", type=int, default=8)
    ap.add_argument("--s1-epochs", type=int, default=10)
    ap.add_argument("--batch", type=int, default=4)
    ap.add_argument("--skip-prep", action="store_true", help="跳过前处理（已跑过就加上）")
    ap.add_argument("--skip-s2", action="store_true", help="跳过 SoVITS（权重已训好时用）")
    ap.add_argument("--version", default="v2", choices=["v2", "v4"], help="底模代次：v2（s2G2333k）或 v4（s2Gv4）")
    args = ap.parse_args()

    data = (ROOT / args.data).resolve()
    opt = (ROOT / "logs" / args.exp).resolve()
    opt.mkdir(parents=True, exist_ok=True)
    meta = data / "metadata.list"
    wavs = data / "raw"
    if not meta.is_file() or not wavs.is_dir():
        raise SystemExit(f"数据缺失：{meta} / {wavs}")

    env = os.environ.copy()
    # 这些脚本用 `from text.cleaner import ...` / `from tools.my_utils import ...`，
    # 包根在 GPT_SoVITS/ 下，必须把它放进 PYTHONPATH，否则 ModuleNotFoundError: text
    env["PYTHONPATH"] = os.pathsep.join(
        [str(GSV / "GPT_SoVITS"), str(GSV), env.get("PYTHONPATH", "")]
    ).strip(os.pathsep)
    # GPT-SoVITS 用 `ffmpeg -nostdin` 直接读 wav/做重采样，Windows 上没有就静默产出空文件
    env["PATH"] = os.pathsep.join([str(ROOT / "tools" / "bin"), env.get("PATH", "")])
    if args.version == "v4":
        s2g = PRE / "gsv-v4-pretrained" / "s2Gv4.pth"
        s2d = ""
        s1_pre = PRE / "s1v3.ckpt"
        weight_dir = "weights_sovits_v4"
        gpt_dir = "weights_gpt_v4"
    else:
        s2g = PRE / "gsv-v2final-pretrained" / "s2G2333k.pth"
        s2d = str(PRE / "gsv-v2final-pretrained" / "s2D2333k.pth")
        s1_pre = PRE / "gsv-v2final-pretrained" / "s1bert25hz-5kh-longer-epoch=12-step=369668.ckpt"
        weight_dir = "weights_sovits_v2"
        gpt_dir = "weights_gpt_v2"

    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "inp_text": str(meta),
            "inp_wav_dir": str(wavs),
            "exp_name": args.exp,
            "opt_dir": str(opt),
            "bert_pretrained_dir": str(PRE / "chinese-roberta-wwm-ext-large"),
            "cnhubert_base_dir": str(PRE / "chinese-hubert-base"),
            "pretrained_s2G": str(s2g),
            "s2config_path": str(GSV / "GPT_SoVITS" / "configs" / "s2.json"),
            "i_part": "0",
            "all_parts": "1",
            "is_half": "1",
            "_CUDA_VISIBLE_DEVICES": "0",
            "hz": "25hz",
        }
    )

    if not args.skip_prep:
        # 这几个脚本只写分片（2-name2text-0.txt / 6-name2semantic-0.tsv），
        # 合并成单文件是 WebUI 在 Python 里做的，这里得自己补上。
        merges = {
            "2-name2text": ".txt",
            "6-name2semantic": ".tsv",
        }
        for script, label, merge_key in [
            ("GPT_SoVITS/prepare_datasets/1-get-text.py", "1/5 文本与 BERT 特征", "2-name2text"),
            ("GPT_SoVITS/prepare_datasets/2-get-hubert-wav32k.py", "2/5 HuBERT 特征", None),
            ("GPT_SoVITS/prepare_datasets/3-get-semantic.py", "3/5 语义 token", "6-name2semantic"),
        ]:
            run([str(PY), "-s", script], env, label)
            if merge_key:
                ext = merges[merge_key]
                parts = sorted(opt.glob(f"{merge_key}-*{ext}"))
                lines: list[str] = []
                for p in parts:
                    lines += [x for x in p.read_text("utf-8").splitlines() if x.strip()]
                (opt / f"{merge_key}{ext}").write_text("\n".join(lines) + "\n", "utf-8")
                for p in parts:
                    p.unlink()
                print(f"合并 {merge_key}{ext}：{len(lines)} 行", flush=True)

    # s2_train/s1_train 会把 checkpoint 写进 <exp>/logs_s2_v2、<exp>/logs_s1_v2，
    # 但不会自己建目录，Windows 上就直接 FileNotFoundError
    for sub in (f"logs_s2_{args.version}", f"logs_s1_{args.version}"):
        (opt / sub).mkdir(parents=True, exist_ok=True)

    tmp = ROOT / "logs" / "tmp_gsv"
    tmp.mkdir(parents=True, exist_ok=True)

    # ── SoVITS ───────────────────────────────────────────────
    cfg = json.loads((GSV / "GPT_SoVITS" / "configs" / "s2.json").read_text("utf-8"))
    cfg["train"].update(
        {
            "batch_size": args.batch,
            "epochs": args.s2_epochs,
            "pretrained_s2G": str(s2g),
            "pretrained_s2D": s2d,
            "if_save_latest": True,
            "if_save_every_weights": True,
            "save_every_epoch": 1,
            "gpu_numbers": "0",
            "fp16_run": True,
        }
    )
    cfg["model"]["version"] = args.version
    cfg["data"]["exp_dir"] = cfg["s2_ckpt_dir"] = str(opt)
    cfg["save_weight_dir"] = str(ROOT / "logs" / weight_dir)
    cfg["name"] = args.exp
    cfg["version"] = args.version
    (ROOT / "logs" / weight_dir).mkdir(parents=True, exist_ok=True)
    s2_path = tmp / "s2.json"
    s2_path.write_text(json.dumps(cfg, ensure_ascii=False), "utf-8")
    if args.skip_s2:
        print("跳过 SoVITS 训练（--skip-s2）", flush=True)
    else:
        run([str(PY), "-s", "GPT_SoVITS/s2_train.py", "--config", str(s2_path)], env, "4/5 SoVITS 训练")

    # ── GPT (s1) ─────────────────────────────────────────────
    import yaml  # 延迟导入：前处理不需要它

    s1 = yaml.safe_load((GSV / "GPT_SoVITS" / "configs" / "s1longer-v2.yaml").read_text("utf-8"))
    s1["train"].update(
        {
            "batch_size": args.batch,
            "epochs": args.s1_epochs,
            "save_every_n_epoch": 1,
            "if_save_every_weights": True,
            "if_save_latest": True,
            "if_dpo": False,
            "half_weights_save_dir": str(ROOT / "logs" / gpt_dir),
            "exp_name": args.exp,
        }
    )
    s1["pretrained_s1"] = str(s1_pre)
    s1["train_semantic_path"] = str(opt / "6-name2semantic.tsv")
    s1["train_phoneme_path"] = str(opt / "2-name2text.txt")
    s1["output_dir"] = str(opt / f"logs_s1_{args.version}")
    (ROOT / "logs" / gpt_dir).mkdir(parents=True, exist_ok=True)
    s1_path = tmp / "s1.yaml"
    s1_path.write_text(yaml.dump(s1, default_flow_style=False, allow_unicode=True), "utf-8")
    run(
        [str(PY), "-s", "GPT_SoVITS/s1_train.py", "--config_file", str(s1_path)],
        env,
        "5/5 GPT 训练",
    )

    print("\n完成。权重位置：")
    for d in ["logs/weights_sovits_v2", "logs/weights_gpt_v2", "logs/" + args.exp]:
        print(" ", d)


if __name__ == "__main__":
    main()
