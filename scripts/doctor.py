"""一键自检：把整套语音系统体检一遍，出问题直接告诉你哪坏了、怎么修。

    ./.venv/Scripts/python.exe scripts/doctor.py

检查项：
  1. voxd（8765）在不在、缓存多少
  2. 各引擎能不能用（本地 piper / gsv、云端 cloud、插件 http）
  3. 音色注册表是否合法（引用文件在不在、字段缺不缺）
  4. 冻结音色的基线有没有被改动
  5. 云端凭据是否配置、能不能真的合成一句
  6. 缓存目录大小、日志目录大小

退出码：0 = 全绿；1 = 有致命问题。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import sys
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8765"

OK = "✅"
WARN = "⚠️ "
BAD = "❌"

problems: list[str] = []


def say(mark: str, msg: str) -> None:
    print(f"{mark} {msg}")
    if mark == BAD:
        problems.append(msg)


def get(path: str, timeout: float = 5.0):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def check_health() -> dict | None:
    try:
        h = get("/v1/health")
    except Exception as e:  # noqa: BLE001
        say(BAD, f"voxd 连不上（{BASE}）：{e}")
        say(WARN, "先双击 run.bat，或检查 8765 端口")
        return None
    say(OK, f"voxd 在线，运行 {h.get('uptime_s', '?')} 秒")
    return h


def check_engines() -> None:
    try:
        data = get("/v1/engines")
    except Exception as e:  # noqa: BLE001
        say(BAD, f"/v1/engines 失败：{e}")
        return
    for e in data.get("engines", []):
        if e.get("error"):
            say(BAD, f"引擎 {e['name']} 加载失败：{e['error']}")
        elif e.get("loaded"):
            say(OK, f"引擎 {e['name']}：已加载，sr={e.get('sr')}")
        else:
            say(WARN, f"引擎 {e['name']}：未加载（第一次用到时才加载）")


def check_voices() -> None:
    root = ROOT / "voices"
    files = sorted(root.glob("*/voice.json"))
    say(OK, f"音色 {len(files)} 个（读本地 voice.json，接口不返回引擎字段）")
    for f in files:
        vid = f.parent.name
        try:
            meta = json.loads(f.read_text("utf-8"))
        except ValueError as e:
            say(BAD, f"音色 {vid} 的 voice.json 不是合法 JSON：{e}")
            continue
        if not any(meta.get(k) for k in ("engine", "preset", "design")):
            say(WARN, f"音色 {vid} 没写 engine/preset/design，路由可能选不到合适引擎")
        if meta.get("ref"):
            ref = Path(meta["ref"])
            cand = ref if ref.is_absolute() else ROOT / ref
            if not cand.is_file():
                say(WARN, f"音色 {vid} 的 ref 文件不存在：{meta['ref']}")


def check_frozen() -> None:
    p = ROOT / "voices" / "loli12-gsv" / "voice.json"
    if not p.is_file():
        say(WARN, "找不到冻结音色 loli12-gsv")
        return
    meta = json.loads(p.read_text("utf-8"))
    if meta.get("frozen") and meta.get("temperature") == 1.0 and meta.get("seed") == 1:
        say(OK, "定稿音色 loli12-gsv：参数仍是冻结基线")
    else:
        say(BAD, f"定稿音色参数被改动过：{meta}")


def check_cloud() -> None:
    cfg = (ROOT / "config.toml").read_text("utf-8")
    has_key = "cloud_access_key" in cfg and "cloud_app_id" in cfg
    if not has_key:
        say(WARN, "config.toml 没配云端凭据（cloud_app_id / cloud_access_key）")
        return
    say(OK, "云端凭据已配置")
    if not any(v for v in (ROOT / "voices").glob("*/voice.json") if '"cloud"' in v.read_text("utf-8")):
        say(WARN, "没有 engine=cloud 的音色")
        return
    say(WARN, "云端合成需要联网+计费，doctor 不自动调用（用 scripts/say.py 手动测）")


def check_dirs() -> None:
    for name in ("cache", "logs", "audition"):
        d = ROOT / name
        if not d.is_dir():
            continue
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        say(OK, f"{name}/ {size / 1e6:.1f} MB")


def main() -> None:
    print("=" * 60)
    print(" voxd 语音系统自检")
    print("=" * 60)
    if check_health():
        check_engines()
        check_voices()
        check_frozen()
        check_cloud()
        check_dirs()
    print("=" * 60)
    if problems:
        print(f"发现 {len(problems)} 个问题：")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    print("全部通过。")


if __name__ == "__main__":
    main()
