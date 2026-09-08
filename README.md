# 🎙️ AI 语音助手 — 小音

基于 **Whisper**（听）+ **DeepSeek**（想）+ **Edge-TTS**（说）的智能语音助手 Web 应用。

## 对话流程

```
用户说话 → 麦克风录音 → Whisper 语音转文字
  → DeepSeek 理解并生成回复 → Edge-TTS 文字转语音 → 扬声器播放
```

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
| ⚙️ 切换音色 | 在设置中选择不同的 TTS 音色（小伊/晓晓/云希等） |
| 🔄 重置对话 | 清空对话历史，开始新话题 |

## 技术栈

| 模块 | 技术 | 说明 |
|------|------|------|
| 🎤 语音识别 | Faster-Whisper (base) | 本地运行，支持中文 |
| 🧠 对话理解 | DeepSeek API | 多轮对话上下文保持 |
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

- [ ] 唤醒词检测（Porcupine / openWakeWord）
- [ ] 流式 LLM + 流式 TTS（边生成边播放，降低延迟）
- [ ] 打断功能（barge-in）
- [ ] Function Calling（查天气、控制智能家居等）
- [ ] WebSocket 服务化架构
- [ ] 端侧部署（whisper.cpp + 嵌入式设备）

## License

MIT
