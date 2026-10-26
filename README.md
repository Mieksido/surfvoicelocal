# voice-local —— 本地语音（7.3 GB）

给桌宠 / QQ / 直播用的**本地**语音后端：GPT-SoVITS 微调音色，免费、断网可用。
端口 **8765**。默认音色 `loli12-gsv`（冻结，不许改）。

> 云端版在隔壁 `../voice-cloud`（端口 8767，264 MB）。两个可以同时跑、分别注入宿主。

```
宿主（QQ / 直播 / 桌宠）
        │  HTTP / NDJSON
        ▼
   voxd  127.0.0.1:8765          路由 · 切片 · 缓存 · 请求日志
        │
        ├── gsv   本地 GPT-SoVITS（age12，1.0~1.5s，免费）   ← 默认
        ├── cloud 火山声音复刻 2.0（age12，0.5~1.1s，按字符计费）
        └── piper 本地 ONNX（兜底，0.2s）
```

---

## 现在的状态（2026-10）

| 音色 id | 引擎 | 用途 | 延迟 |
|---|---|---|---|
| **`loli12-gsv`** | gsv（本地微调） | **默认，QQ 聊天** | 1.0~1.5s |
| `age12-cloud` | cloud（火山复刻） | 直播/成片，质量最高 | 0.5~1.1s |
| `huayan` | piper | 兜底（gsv 挂了也能出声） | 0.2s |

其他引擎（`qwen3` / `cosy`）的模型已删除，代码保留在 `voxd/engine/` 备查，`_specs` 里不再注册。

---

## 启动

```bat
run.bat          :: 先起 GPT-SoVITS（:9880），再起 voxd（:8765）
stop.bat         :: 两个都停
```

> 必须用 `Start-Process` / `.bat` 起，**不能挂在本会话的后台任务里**——会话结束会被杀。

自检：

```bat
.venv\Scripts\python.exe scripts\doctor.py
```

它检查：服务在线 → 引擎加载 → 音色参数 → 冻结基线 → 云端凭据 → 磁盘。

---

## 试一句

```bat
curl -s -X POST http://127.0.0.1:8765/v1/speak/file -H "content-type: application/json" ^
     -d "{\"text\":\"你好呀\"}" -o out.pcm
```

返回**裸 PCM**（16bit LE、24kHz）。要 wav 自己加个头：

```bat
ffmpeg -f s16le -ar 24000 -ac 1 -i out.pcm out.wav
```

命令行更省事：

```bat
.venv\Scripts\python.exe scripts\say.py "一句话"                      :: 默认 loli12-gsv
.venv\Scripts\python.exe scripts\say.py "一句话" --voice age12-cloud  :: 云端
.venv\Scripts\python.exe scripts\say.py --batch lines.txt --out out   :: 整篇配音
```

试听台：<http://127.0.0.1:8765/audition/>（或直接开 `audition/index.html`）。

---

## HTTP 接口

| 端点 | 说明 |
|---|---|
| `POST /v1/speak` | NDJSON 流：`start` → 多个 `seg` → `end`（带 `duration_ms`、`cached`、`engine`） |
| `POST /v1/speak/file` | 一次性返回裸 PCM（16bit LE，24kHz） |
| `GET /v1/health` | 存活 |
| `GET /v1/voices` | 音色列表 |
| `GET /v1/engines` | 引擎列表（`loaded` / `error` / 真实 caps） |
| `POST /v1/stop` | 中断当前合成（直播打断用） |

请求体：

```json
{ "text": "要说的话[笑]", "voice": "loli12-gsv", "character": "xiaoyu",
  "split": "clause", "sr": 24000, "cache": true }
```

- `voice` 显式指定音色；不传则走 `[voice].default` / 角色映射
- `split`：`clause`（按标点切）或 `none`（整句一口气）；**音色里配了就以音色为准**
- `[笑]` 这类标签会被 `markup` 解析成语气

---

## 配置（`config.toml`）

```toml
[server]
port = 8765

[engine]
primary = "gsv"          # 默认引擎
fallbacks = ["piper"]    # 前面挂了就退到这里（strict 音色不走兜底）
gsv_url = "http://127.0.0.1:9880"

[cloud]
app_id = "..."
access_key = "..."
resource = "seed-icl-2.0"

[voice]
default = "loli12-gsv"
```

> 新增引擎可以完全不改代码，见 `[engines.<name>]` 与 `docs/ARCHITECTURE.md`。

---

## 音色（`voices/<id>/voice.json`）

```json
// voices/loli12-gsv —— 默认音色，冻结
{ "engine": "gsv",
  "ref": "voicepack/age12/01.wav",
  "ref_text": "今天天气不错，下午要不要出去走走？",
  "temperature": 1.0, "top_k": 15, "top_p": 1.0, "seed": 1,
  "strict": true, "split": "none", "frozen": true }
```

关键字段：

| 字段 | 作用 |
|---|---|
| `engine` | 指定引擎（配 `strict` 即强制） |
| **`strict`** | 失败时**不发**，绝不换成别的音色 |
| **`split`** | `"none"` = 整句合成（定稿音色必须） |
| **`frozen`** | 定稿标记，测试会挡住参数改动 |
| `temperature` / `top_k` / `seed` | 采样参数，影响音色稳定性 |

> ⚠️ **这个音色的参数不要动**。它听过、确认过、冻结了；改一个数就可能"听起来不是她"。

---

## 踩过的坑（会让声音变人，务必看）

1. **双引擎孪生偷偷换音色**：`cosy_max_chars > 0` 时带标签短句会切到另一个声音。现在为 0；`strict` 音色永不参与。
2. **切句像两个人**：默认按标点切、逐句生成再拼。实测同一句：切句 4.19s/265Hz vs 整句 2.92s/356Hz。定稿音色必须 `split: "none"`。
3. **采样率声明错 = 变慢变低**：GPT-SoVITS 实际 32k，曾写成 24000 → 慢 33%、音高低一档（听感像"变男声"）。现在引擎内部统一重采样到 24k。
4. **失败静默回退**：`strict: false` 时 gsv 挂了会退到 piper（另一个声音）。定稿音色必须 `strict: true`。
5. **缓存留住错声音**：换音色/参数后清 `cache/` 与桥接的 `state/voice-tts/`，并把 `tts.cacheVersion` 加 1。

"声音怎么变了"——先看 `logs/requests.jsonl` 里的 `engine` 字段。

---

## 测试

```bat
.venv\Scripts\python.exe -m pytest tests -q      :: 30 条，全绿
.venv\Scripts\python.exe scripts\doctor.py       :: 一键体检
.venv-gsv\Scripts\python.exe scripts\check_pronunciation.py --port 9880   :: ASR 回读发音
```

---

## 目录

| 路径 | 作用 |
|---|---|
| `voxd/` | 服务本体（api / router / markup / codec / voices / engine/*） |
| `voices/<id>/` | 音色（目录即注册，热加载） |
| `config.toml` | 引擎 / 路由 / 云端凭据 |
| `cache/` | 磁盘 LRU，键含音色指纹 |
| `logs/requests.jsonl` | 每次合成记录（engine / chars / ms） |
| `logs/weights_{sovits,gpt}_v2/` | GPT-SoVITS 训练产物（age12 定稿权重） |
| `dataset/age12/` | 训练数据（`wav\|speaker\|lang\|text`） |
| `voicepack/age12/` | 参考音频样本（云端复刻用） |
| `audition/` | 试听包 |
| `scripts/` | say / doctor / check_pronunciation / sing / audition |
| `docs/ARCHITECTURE.md` | 扩展指南（接新引擎/音色/宿主） |

## 唱歌

已暂停，结论与选型写在 `docs/ARCHITECTURE.md` 5.5 节。

---

## 两个环境（别弄混）

| 环境 | 用途 |
|---|---|
| `.venv`（Python 3.13） | **voxd 本体**、scripts、测试 |
| `.venv-gsv`（Python 3.12） | GPT-SoVITS 训练/推理（api_v2.py 跑在 9880） |
