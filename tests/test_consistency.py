"""12 条一致性测试 —— 「泛用」的验收标准。

这些测试全部用 stub 引擎跑，不需要 GPU、不需要权重。
主引擎装好之后，它们必须仍然全绿。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import write_cfg
from voxd.api import create_app
from voxd.engine.stub import StubEngine
from voxd.router import Engines, plan
from voxd.scheduler import Scheduler
from voxd.schema import Style
from voxd.voices import Voice


def frames(resp):
    assert resp.status_code == 200, resp.text
    return [json.loads(x) for x in resp.text.splitlines() if x.strip()]


def segs(resp):
    return [f for f in frames(resp) if f["type"] == "seg"]


# 1 ── 同样的请求两次：第二次必须走缓存，且字节一致
def test_01_cache_hit(client):
    body = {"text": "你好，世界。", "voice": "a", "format": "wav"}
    r1 = client.post("/v1/speak/file", json=body)
    r2 = client.post("/v1/speak/file", json=body)
    assert r1.status_code == r2.status_code == 200
    assert r1.content == r2.content
    assert r1.headers["X-Cached"] == "0"
    assert r2.headers["X-Cached"] == "1"


# 2 ── 流必以 end 收尾；出错必以 error 收尾，不静默断
def test_02_stream_terminates(client, tmp_path):
    f = frames(client.post("/v1/speak", json={"text": "第一句。第二句。", "voice": "a"}))
    assert f[0]["type"] == "start"
    assert f[-1]["type"] == "end"
    assert f[-1]["dur_ms"] > 0

    bad = TestClient(create_app(write_cfg(tmp_path / "bad", primary="piper")))
    with bad:
        f2 = frames(bad.post("/v1/speak", json={"text": "会失败。", "voice": "a"}))
    assert f2[-1]["type"] == "error" and f2[-1]["error"]


# 3 ── 客户端中途断开：worker 不泄漏，队列继续服务
def test_03_disconnect_then_continue(client):
    with client.stream("POST", "/v1/speak", json={"text": "很长的一段话。" * 12, "voice": "a"}) as r:
        it = r.iter_lines()
        next(it)
    time.sleep(0.4)
    h = client.get("/v1/health").json()
    assert h["queue"]["queued"] == 0
    r2 = client.post("/v1/speak/file", json={"text": "还在吗", "voice": "a", "format": "wav"})
    assert r2.status_code == 200


# 4 ── /stop 能真的打断（句间取消，不是等它跑完）
def test_04_stop_cancels():
    async def scenario():
        sched = Scheduler(4)
        sched.start()
        started = __import__("threading").Event()

        def run(job):
            started.set()
            for _ in range(300):
                if job.cancel.is_set():
                    job.emit({"type": "cancelled"})
                    return
                time.sleep(0.01)
            job.emit({"type": "end"})

        job = await sched.submit(run, 0)
        assert await asyncio.to_thread(started.wait, 2)
        t = time.perf_counter()
        sched.cancel(job.id)
        got = []
        while True:
            fr = await asyncio.wait_for(job.queue.get(), 3)
            if fr is None:
                break
            got.append(fr)
        dt = time.perf_counter() - t
        await sched.stop()
        return dt, got

    dt, got = asyncio.run(scenario())
    assert any(x["type"] == "cancelled" for x in got)
    assert dt < 1.0


# 5 ── 丢一个音色目录进去，不重启就能用
def test_05_hot_add_voice(client, tmp_path):
    b = tmp_path / "voices" / "b"
    b.mkdir(parents=True)
    (b / "ref.wav").write_bytes(b"RIFF0000WAVEfmt ")
    (b / "voice.json").write_text(json.dumps({"ref_text": "另一段。"}), "utf-8")
    ids = [v["id"] for v in client.get("/v1/voices").json()["voices"]]
    assert "b" in ids
    r = client.post("/v1/speak/file", json={"text": "嗨", "voice": "b", "format": "wav"})
    assert r.status_code == 200


# 6 ── 超长拒绝；正常长文按句切分且不吞字
def test_06_split_and_limit(client):
    too_long = "字" * 400
    assert client.post("/v1/speak", json={"text": too_long, "voice": "a"}).status_code == 413

    text = "这是第一句。" * 20
    s = segs(client.post("/v1/speak", json={"text": text, "voice": "a"}))
    assert len(s) > 1
    assert "".join(x["text"] for x in s) == text


# 7 ── 不认识的标签忽略，不 500
def test_07_unknown_tag(client):
    s = segs(client.post("/v1/speak", json={"text": "[不存在的心情]你好。", "voice": "a"}))
    assert s and s[0]["tags"] == ["不存在的心情"]


# 8 ── 空文本 / 纯标点：返回 dur=0，不崩
def test_08_empty(client):
    f = frames(client.post("/v1/speak", json={"text": "。。。", "voice": "a"}))
    assert [x["type"] for x in f] == ["start", "end"]
    assert f[-1]["dur_ms"] == 0


# 9 ── 并发：各流互不串音
def test_09_concurrency(tmp_path):
    cfg = write_cfg(tmp_path)

    async def scenario():
        app = create_app(cfg)
        async with app.router.lifespan_context(app):
            tr = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=tr, base_url="http://t", timeout=60) as c:
                texts = [f"第{i}句话。" for i in range(8)]
                rs = await asyncio.gather(
                    *[c.post("/v1/speak/file", json={"text": t, "voice": "a", "format": "wav"}) for t in texts]
                )
                out = []
                for r, t in zip(rs, texts):
                    meta = json.loads(base64.b64decode(r.headers["X-Segments"]))
                    out.append((r.status_code, meta[0]["text"], t))
                return out

    for status, got, want in asyncio.run(scenario()):
        assert status == 200
        assert got == want


# 10 ── wav 能被标准库解析，时长与声明一致
def test_10_wav_valid(client):
    r = client.post("/v1/speak/file", json={"text": "这是一句用来检查时长的话。", "voice": "a", "format": "wav"})
    assert r.headers["content-type"].startswith("audio/wav")
    with wave.open(io.BytesIO(r.content), "rb") as w:
        dur = w.getnframes() / w.getframerate()
    assert abs(dur * 1000 - int(r.headers["X-Duration-Ms"])) < 60


# 11 ── 英文、数字保留；URL 不念出来
def test_11_normalize(client):
    s = segs(
        client.post(
            "/v1/speak",
            json={"text": "看这里 https://example.com/a?b=1 还有 GitHub 和 2026 年 3 月。", "voice": "a"},
        )
    )
    joined = "".join(x["text"] for x in s)
    assert "http" not in joined and "example.com" not in joined
    assert "GitHub" in joined and "2026" in joined


# 12 ── 引擎不可用：明确 503 + 原因，宿主可降级
def test_12_engine_unavailable(tmp_path):
    app = create_app(write_cfg(tmp_path, primary="piper"))
    with TestClient(app) as c:
        r = c.post("/v1/speak/file", json={"text": "说话", "voice": "a", "format": "wav"})
        assert r.status_code == 503
        assert "引擎" in r.json()["detail"] or "没有引擎" in r.json()["detail"]


# 附加 ── 有 instruct 能力的引擎拿到的是描述，不是数值
def test_13_plan_prefers_instruct():
    eng = Engines({"stub": StubEngine}, "stub", [])
    _, p = plan(eng, Voice(id="a", ref_audio="x", ref_text="y"), Style(tags=["开心"]))
    assert p.instruct and "轻快" in p.instruct
    assert p.rate == 1.0


# 附加 ── 鉴权：给了 token 就必须带；只读端点保持开放
def test_14_auth(tmp_path):
    cfg = write_cfg(tmp_path, token="s3cret")
    with TestClient(create_app(cfg)) as c:
        assert c.get("/v1/health").status_code == 200
        assert c.post("/v1/speak", json={"text": "你好", "voice": "a"}).status_code == 401
        ok = c.post(
            "/v1/speak/file",
            json={"text": "你好", "voice": "a", "format": "wav"},
            headers={"authorization": "Bearer s3cret"},
        )
        assert ok.status_code == 200


# 附加 ── 试音台能打开（静态页，不需要构建）
def test_15_preview(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "voxd" in r.text


# 附加 ── 能力必须在**加载前**就知道（路由先选引擎，之后才 load）
def test_16_caps_before_load():
    from voxd.engine.qwen3 import Qwen3Engine

    cv = Qwen3Engine("models/Qwen3-TTS-12Hz-0.6B-CustomVoice")
    assert cv.caps.presets and "Vivian" in cv.caps.presets
    assert cv.caps.instruct is False, "0.6B CustomVoice 内部会丢 instruct，不能声称支持"
    assert cv.caps.clone is False

    base = Qwen3Engine("models/Qwen3-TTS-12Hz-0.6B-Base")
    assert base.caps.clone is True and not base.caps.presets

    design = Qwen3Engine("models/Qwen3-TTS-12Hz-1.7B-VoiceDesign")
    assert design.caps.design is True


# 附加 ── 句间留白：两句话之间必须真的停一下（以前 gap_ms 只写在 config 里没人读）
def test_18_sentence_gap(tmp_path):
    text = "第一句。第二句。"
    durs = []
    for gap in (0, 200):
        cfg = write_cfg(tmp_path / f"g{gap}")
        cfg.gap_ms = gap
        with TestClient(create_app(cfg)) as c:
            r = c.post("/v1/speak/file", json={"text": text, "voice": "a", "format": "wav"})
            assert r.status_code == 200
            durs.append(int(r.headers["X-Duration-Ms"]))
    assert durs[1] - durs[0] >= 180, f"句间没有留白：{durs}"


def test_19_age12_voice_is_frozen():
    """age12 是用户定稿的声音。它的采样参数一旦被改动，音色就会变——

    这个测试的作用是：以后任何人（包括我自己）改 voice.json 时必须先想清楚。
    """
    import json
    from pathlib import Path

    meta = json.loads(
        (Path(__file__).resolve().parent.parent / "voices" / "loli12-gsv" / "voice.json").read_text("utf-8")
    )
    assert meta.get("engine") == "gsv"
    assert meta.get("frozen") is True, "age12 是冻结音色，frozen 必须为 true"
    assert meta.get("temperature") == 1.0
    assert meta.get("top_k") == 15
    assert meta.get("top_p") == 1.0
    assert meta.get("seed") == 1
    assert meta.get("ref") == "voicepack/age12/01.wav", "参考音频也不能换"
