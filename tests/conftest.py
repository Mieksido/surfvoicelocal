from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from voxd.api import Config, create_app


def write_cfg(tmp_path: Path, primary: str = "stub", fallbacks=(), max_chars: int = 300, token: str = "") -> Config:
    voices = tmp_path / "voices"
    a = voices / "a"
    a.mkdir(parents=True, exist_ok=True)
    (a / "ref.wav").write_bytes(b"RIFF0000WAVEfmt ")
    (a / "voice.json").write_text(
        json.dumps(
            {
                "desc": "测试音色",
                "ref_text": "这是一段测试音频。",
                "default_style": {"tags": [], "intensity": 0.6},
            },
            ensure_ascii=False,
        ),
        "utf-8",
    )
    toml = f"""
[server]
host = "127.0.0.1"
port = 0
token = "{token}"

[engine]
primary = "{primary}"
fallbacks = {json.dumps(list(fallbacks))}
qwen3_path = "models/none"
piper_model = "models/none.onnx"
gap_ms = 40

[voice]
default = "a"

[limits]
max_chars = {max_chars}
queue_max = 8

[cache]
dir = "cache"
max_mb = 4

[ffmpeg]
path = ""
"""
    p = tmp_path / "config.toml"
    p.write_text(toml, "utf-8")
    return Config(p)


@pytest.fixture()
def client(tmp_path):
    cfg = write_cfg(tmp_path)
    app = create_app(cfg)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def dirs(tmp_path):
    return tmp_path
