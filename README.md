# 🎙️ AI 语音助手 — 小音

基于 **Whisper**（听）+ **DeepSeek**（想）+ **Edge-TTS**（说）的智能语音助手 Web 应用。

## 对话流程

```
用户说话 → 麦克风录音 → Whisper 语音转文字
  → DeepSeek 流式生成回复 → 按句切分 → Edge-TTS 逐句合成 → 边生成边播报
```

- **流式播报**：LLM 每生成完一句话立即合成并播放，不等整段回复生成完，体感延迟大幅降低
- **语音打断**：CLI 模式播放时持续监听麦克风，用户一开口立即停止播报进入下一轮；Web 界面提供"停止播报"按钮
- **Function Calling**：小音会自己调用工具——查天气（Open-Meteo 实时数据）、报时间日期、算数（AST 白名单安全计算）、汇率换算
- **对话记忆**：对话自动保存到本地，重启/刷新后恢复上下文，随时可"重置对话"清空
- **免提唤醒**：CLI 模式喊「小音」即可唤醒开始对话（sherpa-onnx 本地关键词检测，毫秒级）

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

CLI 模式启动后进入**待机状态**，喊「**小音**」即唤醒开始对话，对话结束自动回到待机，无需按键。

默认使用 **sherpa-onnx 中文关键词检测**（3.3M 本地模型，毫秒级响应，无需账号、无需联网）：
- 模型文件在 `models/kws-wenetspeech/`（已随仓库提供）
- 唤醒词在 `models/wake_keywords.txt`（拼音格式，可自行添加，如 `x iǎo y īn x iǎo y īn @小音小音`）
- 灵敏度用 `.env` 的 `KWS_KEYWORDS_THRESHOLD` 调整（默认 0.25，误触发多就调高，唤不醒就调低）
- `WAKE_WORD_ENABLED=0` 可关闭

模型缺失时自动回落到 ASR 关键词方案（用现有 Whisper，约 1 秒响应），不影响使用。

## 技术栈

| 模块 | 技术 | 说明 |
|------|------|------|
| 🎤 语音识别 | Faster-Whisper (base) | 本地运行，支持中文 |
| 🧠 对话理解 | DeepSeek API | 多轮对话上下文保持 |
| 🔧 工具调用 | Function Calling | 天气（Open-Meteo）/ 时间 / 计算器 / 汇率 |
| 💾 对话记忆 | 本地 JSON 持久化 | 重启恢复上下文，重置即清空 |
| 🔊 语音合成 | Edge-TTS | 免费，中文自然度高 |
| 🖥️ 前端界面 | Gradio | Web UI，支持麦克风输入 |

## HuggingFace Spaces 部署

1. 在 HuggingFace 创建新 Space，SDK 选 **Gradio**，硬件选 **CPU（免费）**
2. 上传本项目所有文件到 Space
3. 在 Space Settings → Secrets 中添加：
   - `DEEPSEEK_API_KEY`: 你的 DeepSeek API Key
4. Space 会自动构建并启动，获得公网链接

## 项目结构

```
voice_assistant/
├── app.py              # Gradio Web 前端入口
├── main.py             # CLI 交互入口
├── config.py           # 配置管理
├── stt.py              # 语音识别（Faster-Whisper）
├── llm.py              # 大语言模型（DeepSeek API）
├── tts.py              # 语音合成（Edge-TTS）
├── audio_utils.py      # 音频工具（录音/播放）
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

- [ ] 更多工具（快递查询、新闻、日程提醒等）
- [ ] WebSocket 服务化架构
- [ ] 端侧部署（whisper.cpp + 嵌入式设备）
- [ ] Web 端全双工打断（麦克风持续监听，无需点击按钮）

## License

MIT
