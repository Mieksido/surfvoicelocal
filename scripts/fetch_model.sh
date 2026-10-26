#!/usr/bin/env bash
# 从 ModelScope 拉 Qwen3-TTS 权重（国内 4~5MB/s；hf-mirror 只有 ~280KB/s）。
# 用法: bash scripts/fetch_model.sh <repo> <dest>
set -u
REPO="${1:-Qwen/Qwen3-TTS-12Hz-0.6B-Base}"
DEST="${2:-models/Qwen3-TTS-12Hz-0.6B-Base}"
BASE="https://modelscope.cn/models/${REPO}/resolve/master"
FILES=(
  config.json
  generation_config.json
  merges.txt
  model.safetensors
  preprocessor_config.json
  speech_tokenizer/config.json
  speech_tokenizer/configuration.json
  speech_tokenizer/model.safetensors
  speech_tokenizer/preprocessor_config.json
  tokenizer_config.json
  vocab.json
)
mkdir -p "$DEST"
fail=0
for f in "${FILES[@]}"; do
  mkdir -p "$DEST/$(dirname "$f")"
  out="$DEST/$f"
  if [ -s "$out" ]; then
    echo "skip $f ($(stat -c%s "$out") bytes)"
    continue
  fi
  echo "=== $f $(date +%H:%M:%S)"
  if curl -sSL --retry 5 --retry-all-errors -C - -m 1800 -o "$out.part" "$BASE/$f"; then
    mv "$out.part" "$out"
    echo "ok $f $(stat -c%s "$out") bytes"
  else
    echo "FAIL $f"
    fail=1
  fi
done
echo "done fail=$fail at $(date +%H:%M:%S)"
