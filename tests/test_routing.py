"""路由策略测试：引擎加载失败要能降级；双引擎分流只看「短 + 带标签」."""

from __future__ import annotations

import pytest

from pathlib import Path

from conftest import write_cfg
from voxd.api import Synth
from voxd.engine.stub import StubEngine
from voxd.router import Engines, plan
from voxd.schema import Style
from voxd.voices import Voice


class Broken:
    name = "broken"
    caps = StubEngine.caps

    def load(self):
        raise RuntimeError("模拟：worker 没起来")

    def render(self, *a, **k):  # pragma: no cover
        yield None

    def close(self):
        pass


def test_broken_engine_falls_back():
    eng = Engines({"broken": Broken, "stub": StubEngine}, "broken", ["stub"])
    voice = Voice(id="a", ref_audio="x", ref_text="y")
    e, _ = plan(eng, voice, Style())
    assert e.name == "stub", "加载失败的引擎必须让位给下一个，而不是把 503 抛给宿主"


def test_cosy_hint_only_for_short_tagged(tmp_path: Path):
    cfg = write_cfg(tmp_path)
    cfg.cosy_max_chars = 10
    synth = Synth(cfg)
    assert synth._hint("短句", Style(tags=["开心"]), None) == "cosy"
    assert synth._hint("这句真的非常非常长超过十个字了", Style(tags=["开心"]), None) is None
    assert synth._hint("短句", Style(tags=[]), None) is None
    assert synth._hint("短句", Style(tags=["开心"]), "piper") == "piper", "显式指定优先"

    cfg.cosy_max_chars = 0
    assert Synth(cfg)._hint("短句", Style(tags=["开心"]), None) is None, "0 = 关闭分流"


def test_trailing_tag_applies_to_previous_clause():
    from voxd.markup import parse

    assert parse("来了来了[笑]")[0].tags == ["笑"], "句末标签不能落空"
    parts = parse("你好[开心]再见")
    assert parts[0].tags == [] and parts[1].tags == ["开心"], "句中标签仍作用于其后"


def test_twin_routing(tmp_path: Path):
    import json

    cfg = write_cfg(tmp_path)
    cfg.cosy_max_chars = 40
    cosy = tmp_path / "voices" / "a-cosy"
    cosy.mkdir()
    (cosy / "ref.wav").write_bytes(b"RIFF0000")
    (cosy / "voice.json").write_text(json.dumps({"ref_text": "x", "engine": "cosy"}), "utf-8")
    base = tmp_path / "voices" / "a" / "voice.json"
    meta = json.loads(base.read_text("utf-8"))
    meta["cosy_twin"] = "a-cosy"
    base.write_text(json.dumps(meta), "utf-8")

    synth = Synth(cfg)
    v = synth.voices.resolve("a")
    assert synth._pick_voice(v, "短句", Style(tags=["笑"])).id == "a-cosy", "短情绪句走孪生"
    assert synth._pick_voice(v, "长" * 60, Style(tags=["笑"])).id == "a", "长句回到快引擎"
    assert synth._pick_voice(v, "短句", Style(tags=[])).id == "a", "没标签不走 cosy"


def test_20_strict_voice_never_falls_back(tmp_path: Path):
    """定稿音色：引擎挂了就报错，绝不能换一个声音发出去。"""
    import json

    from voxd.router import EngineError, Engines, plan
    from voxd.engine.stub import StubEngine
    from voxd.schema import Style
    from voxd.voices import Voice

    # 只注册一个会失败的引擎
    def boom():
        raise RuntimeError("引擎起不来")

    engines = Engines({"dead": boom, "stub": StubEngine}, primary="dead", fallbacks=["stub"])
    voice = Voice(id="v", ref_audio="/tmp/x.wav", ref_text="hi", engine="dead", strict=True)
    with pytest.raises(EngineError):
        plan(engines, voice, Style())


def test_21_frozen_voice_uses_whole_sentence(tmp_path: Path):
    """定稿音色必须整句合成：切句会让同一句话听起来像两个人。"""
    import json
    from pathlib import Path as P

    meta = json.loads((P(__file__).resolve().parent.parent / "voices" / "loli12-gsv" / "voice.json").read_text("utf-8"))
    assert meta.get("split") == "none", "定稿音色必须 split=none"
    assert meta.get("strict") is True, "定稿音色必须 strict=true"
