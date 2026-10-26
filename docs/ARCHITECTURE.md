# voxd 架构与扩展指南

> 一句话：**voxd 只做三件事——按能力选引擎、把文本切成小句、把音频流出去。**
> 所以接新东西永远是"在旁边加"，不用改中间那层。

```
┌─────────────┐   HTTP/NDJSON    ┌──────────────┐   引擎协议    ┌──────────────────┐
│  宿主们      │ ───────────────► │    voxd      │ ────────────► │     引擎们        │
│ QQ / 直播    │                  │   8765       │               │ piper（0.2s）     │
│ 视频 / 宠物  │ ◄─────────────── │ 路由·缓存·切片 │ ◄──────────── │ cloud（0.5~1.1s） │
└─────────────┘     pcm 流        └──────────────┘    float32     │ gsv（1.0~1.5s）   │
                                                                  │ http 插件（任意）  │
                                                                  └──────────────────┘
```

---

## 0. 当前在用什么（速查）

| 场景 | 音色 id | 引擎 | 延迟 | 成本 |
|---|---|---|---|---|
| **QQ 聊天（默认）** | `loli12-gsv` | 本地 GPT-SoVITS | 1.0~1.5s | 免费 |
| **直播 / 视频成片** | `age12-cloud` | 火山声音复刻 2.0 | 0.5~1.1s | 按字符 |
| 断网兜底 | 同上本地 | 本地 | — | 免费 |

启动顺序：`run.bat`（gsv + voxd）→ QQ 桥接。

---

## 1. 扩展点一：引擎（怎么把文本变成声音）

### 1.1 内置引擎（当前注册的）

| 名字 | 类型 | 特点 | 延迟 |
|---|---|---|---|
| `gsv` | GPT-SoVITS 本地微调 | 默认；age12 定稿音色 | 1.0~1.5s |
| `cloud` | 火山声音复刻 2.0 | 质量最好、发音最准，按字符计费 | 0.5~1.1s |
| `piper` | 本地 ONNX | 0.2s 出声，无情绪 | 0.2s |
| `stub` | 测试 | 正弦波，跑测试不碰模型 | — |

已移除：`qwen3` / `qwen3-base`（离线太慢，6~28s）、`cosy`（CosyVoice2 情绪引擎，RTF 1.3~5.9）。
代码保留在 `voxd/engine/` 备查，模型已删、不再注册。

### 1.2 加引擎 A：HTTP 插件（不写 voxd 代码）

```
GET  {url}/health        → 200（可选；404 也算服务在）
POST {url}/say           ← {"text","voice":{...},"lang","instruct","rate","pitch"}
                         → 200 + wav 字节，或裸 PCM + 响应头 X-Sample-Rate
```

三步：

```bash
python examples/tts_service.py --port 9900      # 1) 起服务（最小示例，可照抄）
```

```toml
# 2) config.toml 声明能力
[engines.mydemo]
kind = "http"
url = "http://127.0.0.1:9900"
clone = false
instruct = false
langs = ["zh", "en"]
max_chars = 500
sr = 24000
timeout = 120
headers = { Authorization = "Bearer xxx" }   # 可选
```

```json
// 3) voices/demo/voice.json
{ "engine": "mydemo" }
```

重启 voxd 后 `curl 127.0.0.1:8765/v1/engines` 就能看到 `mydemo`。
`tests/test_plugin_engine.py` 里有个假服务，可当回归测试模板。

### 1.3 加引擎 B：内置类

```python
class MyEngine:
    name = "myengine"
    caps = Caps(clone=False, instruct=False, langs=("zh",), max_chars=200, sr=24000)

    def load(self) -> None: ...     # 起服务/加载权重；失败抛 EngineError
    def unload(self) -> None: ...
    def close(self) -> None: ...
    def render(self, text, voice, lang="zh", instruct=None, rate=1.0, pitch=0.0):
        yield np.float32_array   # 幅度 [-1,1]
```

再在 `voxd/api.py::_specs()` 加一行 `"myengine": lambda: MyEngine(...)`。

### 1.4 `Caps` 字段（路由靠它挑引擎）

| 字段 | 含义 |
|---|---|
| `clone` / `instruct` / `presets` / `design` | 支持参考音频 / 语气指令 / 内置说话人 / 描述生成 |
| `langs` / `max_chars` / `sr` | 语言、单次上限、输出采样率 |
| `rate_control` | 自己会变速（否则上层重采样兜底） |
| `instruct_mode` | `description` / `imperative` |

---

## 2. 扩展点二：音色（谁在说话）

**目录即注册**：`voices/<id>/voice.json`，自动发现 + **热加载**。

| 字段 | 作用 |
|---|---|
| `engine` | 建议引擎（配 `strict` 即强制） |
| `desc` | 说明，列表/试听台显示 |
| `ref` / `ref_audio` / `ref_text` | 参考音频 + 逐字文本（克隆类必需） |
| `preset` / `design` / `speaker` | 内置说话人 / 描述生成 / 云端复刻 ID |
| `temperature` / `top_k` / `top_p` / `seed` | 采样参数（影响**音色稳定性**） |
| `speed` / `default_style` | 语速 / 默认语气 |
| `cosy_twin` / `fast_twin` | 双引擎孪生（**已停用**，见 4.1） |
| **`strict`** | `true` = 只许用 `engine` 指定引擎；失败**不发**而不是换人 |
| **`split`** | `"none"` = 整句一口气合成（定稿音色必须） |
| **`frozen`** | `true` = 定稿音色，测试会挡住参数改动 |

两个定稿音色：

```json
// voices/loli12-gsv（QQ，本地）
{ "engine": "gsv", "ref": "voicepack/age12/01.wav", "ref_text": "...",
  "temperature": 1.0, "top_k": 15, "top_p": 1.0, "seed": 1,
  "strict": true, "split": "none", "frozen": true }
```

```json
// voices/age12-cloud（直播/成片）
{ "engine": "cloud", "speaker": "S_fxkbW5bj2" }
```

---

## 3. 扩展点三：宿主（谁调用）

| 端点 | 说明 |
|---|---|
| `POST /v1/speak` | NDJSON 流式：`start`(sr/引擎) → `seg`(逐句+engine) → `end`(dur_ms/cached) |
| `POST /v1/speak/file` | 一次性返回裸 PCM（16bit LE，24k） |
| `GET /v1/health` `/v1/voices` `/v1/engines` | 存活 / 音色 / 引擎（含错误和真实 caps） |
| `POST /v1/stop` | 中断当前合成（直播打断） |

```json
{ "text": "要说的话[笑]", "voice": "loli12-gsv" | "age12-cloud",
  "character": "xiaoyu", "split": "clause", "sr": 24000, "cache": true,
  "style": {"tags": [], "intensity": 0.6} }
```

命令行配音：

```bash
python scripts/say.py "一句话" --voice age12-cloud
python scripts/say.py --batch lines.txt --voice age12-cloud --out out
```

直播：直接用 `examples/live_tts.py`——它就是上面这套的实现（预取队列 + 缓冲监控 + 保底切换）：

```bash
python examples/live_tts.py --script lines.txt --voice age12-cloud     --fallback loli12-gsv --out live_out --lead 20
```

实测：云端做主音色时，缓冲见底会自动切到本地 gsv（日志里能看到 voice 变化），
不会断流。切句/并发合成都在它里面处理好了。

---

## 4. 踩过的坑（这几个会让"声音换人"）

1. **双引擎孪生偷偷换音色**：`cosy_max_chars > 0` 时带标签短句会换到 `cosy_twin`。
   现在为 0；且 `strict` 音色永不参与替换。
2. **切句让一句话像两个人**：默认按标点切、逐句独立生成再拼。
   实测同一句：切句 4.19s/265Hz vs 整句 2.92s/356Hz。定稿音色必须 `split: "none"`。
3. **采样率声明错 = 变慢变低**：GPT-SoVITS 实际 32k，曾经写成 24000 → 慢 33%、音高低一档
   （听感就是"变男声"）。现在 gsv 内部统一重采样到 24k。
4. **失败静默回退**：`strict: false` 时 gsv 不可用会退到 qwen3（另一个声音）。
   定稿音色必须 `strict: true`。
5. **缓存留住错声音**：换音色/参数后必须清 `cache/` 和桥接 `state/voice-tts/`，
   并把 `tts.cacheVersion` 加 1。

---

## 5. 自检与测试

```bash
python scripts/doctor.py                              # 服务/引擎/音色/冻结/凭据/磁盘
pytest tests -q                                       # 30 条：路由·切片·缓存·冻结·插件
python scripts/check_pronunciation.py --port 9880     # ASR 回读发音（客观下限）
```

**请求日志** `logs/requests.jsonl`（"声音怎么变了"先看这里）：

```json
{"t":"2026-10-25T08:42:53","voice":"loli12-gsv","engine":"gsv","chars":47,"ms":9740,"degraded":[]}
```

---

## 5.5 唱歌：结论与方案（2026-10 定稿）

**结论：本地"零样本歌声合成"不可用于生产。**

| 尝试 | 结果 |
|---|---|
| SoulX-Singer（零样本 SVS，704M） | 流程跑通，但**咬字不清**（ASR 只能听出零散字），音色也偏 |
| Seed-VC f0（SVC，换声） | 质量可用：音高相关 0.93、偏差 0.61 半音，**但需要一段真实演唱当源** |
| 手写谱面（简谱图 → 音符） | 时值靠估，节奏一错就"锯木头" |
| RVC / So-VITS-SVC | `fairseq` 在 Python 3.13 + Windows 装不上 |

**选定方案 A（云端生成 + 本地换声）**：

```
歌词 + 风格 → 云端音乐生成（MiniMax Music ≈ $0.15/首，或火山 Seed-Music）
           → demucs 分离人声 / 伴奏（本地，免费）
           → Seed-VC f0 把人声换成 age12（本地，RTF 0.27）
           → 混回伴奏 → 成品
```

用户只需给歌词，不需要每次录音。**当前暂停在此**，等接入 MiniMax/火山后再做。

现有脚本（都已跑通，留作工具）：
- `scripts/sing.py` —— SVC 换声（源演唱 → 目标音色）
- `scripts/hum_to_meta.py` —— 哼唱录音 → 乐谱元数据
- `scripts/build_lyric_meta.py` —— 歌词 + 自拟旋律 → 乐谱元数据
- 试听样本归档：`audition/singing/`、`audition/archive/`

## 6. 目录

| 路径 | 作用 |
|---|---|
| `voxd/` | 服务本体（api / router / markup / codec / voices / engine/*） |
| `voices/<id>/` | 音色（目录即注册） |
| `config.toml` | 引擎/路由/云端凭据/`[engines.*]` 插件 |
| `cache/` | 磁盘 LRU，键含音色指纹 |
| `logs/requests.jsonl` | 每次合成记录 |
| `logs/weights_{sovits,gpt}_v2/` | GPT-SoVITS 训练产物 |
| `dataset/<exp>/` | 训练数据（`wav|speaker|lang|text`） |
| `audition/` | 试听包（`scripts/audition.py --rebuild-index`） |
| `examples/tts_service.py` | 外部 TTS 插件最小示例 |
| `docs/ARCHITECTURE.md` | 本文 |

---

## 7. 接新插件检查清单

1. 服务实现 `POST /say`（wav 或 PCM + `X-Sample-Rate`）
2. `config.toml` 加 `[engines.xxx]`，**能力声明按真实情况填**
3. `voices/<id>/voice.json` 指向 xxx
4. `python scripts/doctor.py` 看引擎/音色是否正常
5. `python scripts/say.py "测试" --voice <id>` 听一句
6. 定稿音色：加 `strict` + `split: "none"` + 跑 `check_pronunciation.py`
