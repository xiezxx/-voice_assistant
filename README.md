# 🎙️ AI 语音助手 — 小音

基于 **Whisper**（听）+ **DeepSeek**（想）+ **Edge-TTS**（说）的智能语音助手 Web 应用。

## 对话流程

```
用户说话 → 麦克风录音 → Whisper 语音转文字
  → DeepSeek 流式生成回复 → 按句切分 → Edge-TTS 逐句合成 → 边生成边播报
```

- **流式播报**：LLM 每生成完一句话立即合成并播放，不等整段回复生成完，体感延迟大幅降低
- **语音打断**：CLI 模式播放时持续监听麦克风，用户一开口立即停止播报进入下一轮；Web 界面提供"停止播报"按钮
- **Function Calling**：小音会自己调用工具——查天气/空气质量（Open-Meteo 实时数据）、报时间日期、算数（AST 白名单安全计算）、汇率换算、新闻快讯（60秒读懂世界）、日程提醒（本地持久化，到点主动提醒）、快递物流（快递鸟，可选 Key）
- **对话记忆**：对话自动保存到本地，重启/刷新后恢复上下文，随时可"重置对话"清空
- **免提唤醒**：喊「小音」即可唤醒开始对话（sherpa-onnx 本地关键词检测，毫秒级）；CLI、Web 界面、任意设备（client.py）均支持
- **全双工打断**：AI 播报时直接开口说话即打断并开启新问题，无需按键（/ws/assistant 全双工协议）

## 快速开始

### 1. 安装依赖
```bash
pip install -r requirements.txt
```

### 2. 配置 API Key
编辑 `.env` 文件：
```
DEEPSEEK_API_KEY=sk-你的key
```

### 3. 启动 Web 界面
```bash
python app.py
# 浏览器打开 http://127.0.0.1:7860
```

首次运行会自动下载 Whisper 模型（base 约 150MB）。

### CLI 模式（终端交互）
```bash
python main.py           # 启动语音助手
python main.py --devices  # 查看可用音频设备
python main.py --voices   # 查看可选 TTS 音色
```

## 操作说明

| 方式 | 操作 |
|------|------|
| 🎤 语音输入 | 点击麦克风按钮，说话后再次点击停止，AI 自动回复 |
| ⌨️ 文字输入 | 在文本框输入文字，点击发送 |
| ⏹ 停止播报 | AI 播报途中点击，立即停止语音（对话文字保留） |
| ⚙️ 切换音色 | 在设置中选择不同的 TTS 音色（小伊/晓晓/云希等） |
| 🔄 重置对话 | 清空对话历史，开始新话题 |

CLI 模式下，AI 播报时**直接开口说话**即可打断，无需按键。

## 免提唤醒

### CLI 模式

CLI 模式启动后进入**待机状态**，喊「**小音**」即唤醒开始对话，对话结束自动回到待机，无需按键。

### Web 界面

打开页面后点击「🎙️ 免提唤醒开关」并**允许浏览器使用麦克风**（一次性授权，浏览器安全要求），之后页面持续监听：

- 说「**小音**」→ 提示音 → 直接说出问题，停顿约 1 秒自动结束录音
- 回复逐句显示在对话区并自动播报，播报完自动回到聆听
- **AI 播报中直接说话即可打断**（全双工），无需任何按键，打断后继续说新问题即可（无需再次唤醒）
- 再次点击开关或刷新页面即关闭（刷新后需重新点击授权）

### 桌面 / 其他设备（CLI 客户端）

服务化后任意设备都可接入（服务器跑在 `python app.py` 的机器上）：

```bash
python client.py                      # 本机接入
python client.py --host 192.168.1.8   # 局域网其他机器接入（防火墙放行 TCP 7860）
python client.py --voice zh-CN-YunxiNeural  # 指定音色
```

按键：`r` 重置对话，`q` 退出；播报中直接说话即可打断。

## 本地 KWS 模型

CLI 与 Web 唤醒共用同一套模型：

默认使用 **sherpa-onnx 中文关键词检测**（3.3M 本地模型，毫秒级响应，无需账号、无需联网）：
- 模型文件在 `models/kws-wenetspeech/`（已随仓库提供）
- 唤醒词在 `models/wake_keywords.txt`（拼音格式，可自行添加，如 `x iǎo y īn x iǎo y īn @小音小音`）
- 灵敏度用 `.env` 的 `KWS_KEYWORDS_THRESHOLD` 调整（默认 0.25，误触发多就调高，唤不醒就调低）
- `WAKE_WORD_ENABLED=0` 可关闭

模型缺失时自动回落到 ASR 关键词方案（用现有 Whisper，约 1 秒响应），不影响使用。

## 接入协议（WebSocket /ws/assistant）

服务器通过单一全双工 WebSocket 协议提供语音会话能力，浏览器、CLI 客户端、未来设备共用：

**客户端 → 服务器**
| 消息 | 说明 |
|------|------|
| 二进制帧 | int16 LE 单声道 16k PCM，任意帧大小；**持续推流**（含 AI 播报期间，供服务器检测打断） |
| `{"type":"hello","voice":"..."}` | 可选：会话初始化，音色覆盖服务器全局 |
| `{"type":"text","text":"..."}` | 文字输入轮次 |
| `{"type":"stop"}` | 停止当前播报 |
| `{"type":"reset"}` | 重置对话上下文 |

**服务器 → 客户端**
| 事件 | 说明 |
|------|------|
| `{"type":"ready","sample_rate":16000,"chunk":1600,"kws":true}` | 连接建立 |
| `{"type":"wake","keyword":"小音"}` | 检测到唤醒词（客户端本地响提示音），每连接 5s 冷却 |
| `{"type":"transcript","text":"..."}` | 用户话语识别结果 |
| `{"type":"status","text":"..."}` | 工具调用状态 |
| `{"type":"sentence","text":"..."}` | LLM 逐句文本 |
| `{"type":"audio","format":"mp3"}` + 一个二进制帧 | 该句 TTS 音频（二进制帧 = 完整 mp3） |
| `{"type":"barge_in"}` | 检测到用户说话，客户端停止播放；服务器已开始聆听新话语 |
| `{"type":"turn_end","status":"...","stopped":false}` | 轮次结束 |
| `{"type":"error","error":"..."}` | 错误 |

**服务器会话状态机**（每连接）：

```
IDLE(KWS检测) ──「小音」──► 发 wake ──► LISTENING(VAD采集) ──► transcript ──► REPLYING
  ▲                                                                            │
  │ turn_end / stop  ←──────────────────────────────────────────────────────────┤
  │                                    （LLM流式→逐句 sentence+mp3 下行）         │
  └────────────────────────── 检测到用户说话 → barge_in → 直接新一轮 LISTENING ──┘
```

最简 Python 接入示例：

```python
import asyncio, json, numpy as np, websockets

async def main():
    async with websockets.connect("ws://127.0.0.1:7860/ws/assistant") as ws:
        print(await ws.recv())  # {"type":"ready",...}
        # 推麦克风音频（int16 16k PCM）
        await ws.send(np.zeros(1600, dtype=np.int16).tobytes())
        async for msg in ws:  # JSON 事件 / mp3 二进制帧
            ...
asyncio.run(main())
```

说明：对话上下文为**服务器全局共享**（所有客户端看到同一对话）；音色可按连接覆盖；回复串行执行；打断检测的 0.3s 语音不进入新话语采集（首音节可能轻微截断）。

## 技术栈

| 模块 | 技术 | 说明 |
|------|------|------|
| 🎤 语音识别 | Faster-Whisper (base) | 本地运行，支持中文 |
| 🧠 对话理解 | DeepSeek API | 多轮对话上下文保持 |
| 🔧 工具调用 | Function Calling | 天气/空气质量（Open-Meteo）/ 时间 / 计算器 / 汇率 / 新闻快讯 / 日程提醒 / 快递（可选） |
| 💾 对话记忆 | 本地 JSON 持久化 | 重启恢复上下文，重置即清空 |
| ⏰ 日程提醒 | 本地 JSON（data/reminders.json） | 到点后在下一轮对话主动提醒，可查可删 |
| 🔊 语音合成 | Edge-TTS | 免费，中文自然度高 |
| 🖥️ 前端界面 | Gradio | Web UI，支持麦克风输入 |

### 快递查询（可选配置）

快递工具使用**快递鸟**免费接口（其余工具全部零账号、零 Key）：

1. 到 https://www.kdniao.com 免费注册，申请「即时查询」接口（RequestType 1002）
2. 在 `.env` 填写 `KDNIAO_EBUSINESS_ID`（商户ID）和 `KDNIAO_APP_KEY`（接口Key）
3. 之后说「查一下顺丰 SF1234567890」即可；未配置时小音会提示，不影响其他功能

## HuggingFace Spaces 部署

1. 在 HuggingFace 创建新 Space，SDK 选 **Gradio**，硬件选 **CPU（免费）**
2. 上传本项目所有文件到 Space
3. 在 Space Settings → Secrets 中添加：
   - `DEEPSEEK_API_KEY`: 你的 DeepSeek API Key
4. Space 会自动构建并启动，获得公网链接

## 项目结构

```
voice_assistant/
├── app.py              # Gradio Web 前端入口（含免提唤醒 UI 与 Timer 播报）
├── main.py             # CLI 交互入口（含免提唤醒）
├── config.py           # 配置管理
├── stt.py              # 语音识别（Faster-Whisper）
├── llm.py              # 大语言模型（DeepSeek API）
├── tts.py              # 语音合成（Edge-TTS）
├── tools.py            # Function Calling 工具（天气/时间/计算器/汇率）
├── audio_utils.py      # 音频工具（录音/播放/打断）
├── wakeword.py         # 唤醒词检测（sherpa-onnx KWS / ASR 兜底）
├── voice_server.py     # 全双工语音会话服务端（/ws/assistant 协议 + 会话状态机）
├── client.py           # CLI 薄客户端（任意设备接入服务器对话）
├── web/wake_mode.js    # 浏览器端全双工会话脚本（推流/播放/打断）
├── speech_utils.py     # 句子切分等语音工具
├── conversation_store.py  # 对话持久化
├── models/             # KWS 模型与唤醒词文件（已随仓库提供）
├── requirements.txt    # Python 依赖
├── .env                # API Key 配置（需自行填写）
├── .env.example        # 配置模板
└── .gitignore
```

## 配置参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| WHISPER_MODEL | base | 模型大小：tiny/base/small/medium/large-v3 |
| WHISPER_DEVICE | cpu | 推理设备：cpu 或 cuda |
| VAD_THRESHOLD | 0.02 | 语音活动检测灵敏度 |
| SILENCE_DURATION | 1.0 | CLI 模式静音判定秒数 |

## 后续扩展方向

- [x] 更多工具（快递查询、新闻快讯、日程提醒、空气质量）
- [x] Web 端全双工打断（免提唤醒，无需点击按钮）
- [x] WebSocket 服务化架构（/ws/assistant 统一协议 + 浏览器/CLI 双客户端）
- [ ] 桌面 3D 宠物客户端（常驻桌面、随时唤醒）
- [ ] 更多工具（股票基金、农历节日、备忘录等）
- [ ] 端侧部署（whisper.cpp + 嵌入式设备）

## License

MIT
