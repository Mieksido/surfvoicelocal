# 交接：语音系统现状（2026-10 整理版）

> 一句话：**系统已整理干净，默认音色冻结不变，随时可以接。**

## 一、当前状态

| 项 | 现状 |
|---|---|
| 磁盘 | **7.3 GB**（整理前 61 GB） |
| 引擎 | 4 个：`gsv`（默认）/ `cloud` / `piper` / `stub`（测试） |
| 音色 | 3 个：`loli12-gsv`（默认）/ `age12-cloud` / `huayan`（兜底） |
| 服务 | voxd :8765 在线；GPT-SoVITS :9880 在线 |
| 测试 | 30 条全绿 |
| 体检 | `doctor.py` 全绿 |

实测三种音色（刚跑过）：

| 音色 | 耗时 | F0 |
|---|---|---|
| `loli12-gsv`（默认） | 2.90s | 343Hz |
| `age12-cloud` | 1.81s | 387Hz |
| `huayan`（兜底） | 1.32s | 273Hz |

## 二、空间去哪了（61 GB → 7.3 GB）

### 现在只占 7.3 GB，构成如下

| 目录 | 大小 | 是什么 | 能不能再删 |
|---|---|---|---|
| `.venv-gsv` | 4.0G | GPT-SoVITS 运行环境（torch 2.7G + 依赖） | ❌ 推理必需 |
| `vendor` | 1.9G | GPT-SoVITS 代码 + v2 预训练 + 中文前端（G2PW/roberta/hubert） | ❌ 合成必需 |
| `.venv` | 1.1G | voxd 本体（fastapi/onnxruntime/numpy/scipy） | ❌ 服务必需 |
| `logs` | 241M | age12 定稿权重 245M + 请求日志 | ⚠️ 权重不能删 |
| `audition` | 24M | 试听样本 | ✅ 可删 |
| `models` | 61M | piper 兜底模型 | ⚠️ 兜底用 |

### 一共删掉了 53 GB

| 删除 | 大小 | 原因 |
|---|---|---|
| SoulX-Singer（模型+代码+环境） | 8.6G | 唱歌实验失败，已放弃 |
| CosyVoice2（模型+环境+代码） | 7.7G | 情绪引擎，RTF 1.3~5.9，从没启用 |
| Seed-VC（模型+代码+环境） | 6.7G | 唱歌换声，方案暂停 |
| Qwen3-TTS 三个模型 | 9.1G | 离线太慢（6~28s），已被 gsv 取代 |
| 训练中间产物（age12v2/v3/v4） | 6.1G | 残渣，推理不需要 |
| v4 失败权重 | 1.85G | 输出是噪声 |
| 下载缓存（wheels/checkpoints） | 4.3G | 装完就没用了 |
| GPT-SoVITS 重复预训练拷贝 | 1.5G | `hubert/roberta/gpt` 与 `code/.../pretrained_models` 重复 |
| G2PW 压缩包 | 562M | 已解压，zip 多余 |
| 散落的实验 wav / 旧权重 / 失效音色 | — | 只保留定稿 |

### voxd 环境瘦身（4.2G → 1.1G）

`.venv` 里的 `torch`（2.7G）只是为了已删除的 qwen3 引擎，一并卸掉了
（`torch/triton/bitsandbytes/transformers/gradio/pyarrow/numba/llvmlite`），
并移除了 `api.py` 里那行 `from .engine.qwen3 import Qwen3Engine`。

### 还想要更小？

只剩一个方向：给 `.venv-gsv` 换 CPU 版 torch（-2G），代价是合成从 1 秒变 10 秒以上——**不建议**。

## 三、接入 surfcore

桥接侧已经接好了（`src/voice-tts.js` / `src/bridge.js`），只需确认三处：

```js
// surfcore 配置里
voice: {
  baseUrl: "http://127.0.0.1:8765",
  default: "loli12-gsv",     // 必须是这个
  tts: { cacheVersion: 4 }   // 换音色/参数时才需要 +1
}
```

验证：

```bash
cd D:\deepseekwork\surfcore && node scripts/voice-live-check.mjs
```

期望：`✅ 桥接已认识 text：合成 xxx ms 音频`（dryRun，不发消息）。

真机发一条：

```bash
node scripts/voice-send-test.mjs --key group:100000004 --yes
```

## 四、日常操作

```bat
run.bat        :: 启动（先 gsv 后 voxd）
stop.bat       :: 停止
.venv\Scripts\python.exe scripts\doctor.py    :: 体检
.venv\Scripts\python.exe -m pytest tests -q   :: 测试
```

> 必须用 `Start-Process` / `.bat` 起服务；挂在会话后台任务里会随会话结束被杀。

## 五、唱歌：已暂停

结论写在 `docs/ARCHITECTURE.md` 5.5 节：
- 本地零样本合成（SoulX）咬字不清，**不用于生产**
- 本地换声（Seed-VC）质量可用，但每次都需要一段真实演唱
- **选定方案 A**（未实施）：云端音乐生成（MiniMax ≈$0.15/首 或 火山 Seed-Music）→ demucs 分人声 → Seed-VC 换声 → 混音
- 恢复时需要：MiniMax API key 或 火山音乐服务开通

## 六、已知限制

1. **音色不许动**：`voices/loli12-gsv/voice.json` 的参数是冻结基线，`pytest` 会挡住改动。
2. **gsv 挂了会静音**（`strict: true` 宁可不发也不换人），这是设计选择；要说话兜底就显式传 `voice: "huayan"`。
3. **云端按字符计费**，3 小时/天 ≈ 135 万字符/月，费用要自己评估。
4. 缓存两层（voxd `cache/`、桥接 `state/voice-tts/`），换参数后两层都要清。
