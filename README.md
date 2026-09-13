# 🎙️ AI 语音助手 — 小音

基于 **Whisper**（听）+ **DeepSeek**（想）+ **Edge-TTS**（说）的智能语音助手 Web 应用。

## 对话流程

```
用户说话 → 麦克风录音 → Whisper 语音转文字
  → DeepSeek 流式生成回复 → 按句切分 → Edge-TTS 逐句合成 → 边生成边播报
```

- **流式播报**：LLM 每生成完一句话立即合成并播放，不等整段回复生成完，体感延迟大幅降低
- **语音打断**：CLI 模式播放时持续监听麦克风，用户一开口立即停止播报进入下一轮；Web 界面提供"停止播报"按钮
- **Function Calling**：小音会自己调用工具——查天气/空气质量（Open-Meteo 实时数据）、报时间日期、农历节气节日（本地计算）、算数（AST 白名单安全计算）、汇率换算、新闻快讯（60秒读懂世界）、日程提醒（到点主动提醒）、备忘录（本地持久化）、快递物流（快递鸟，可选 Key）、QQ 音乐点歌与播放控制（本机装有客户端即可）
- **对话记忆**：对话自动保存到本地，重启/刷新后恢复上下文，随时可"重置对话"清空
- **免提唤醒**：喊「小音」即可唤醒开始对话（sherpa-onnx 本地关键词检测，毫秒级）；CLI、Web 界面、任意设备（client.py）均支持
- **全双工打断**：AI 播报时直接开口说话即打断并开启新问题，无需按键（/ws/assistant 全双工协议）
- **声纹锁定**：识别并记忆主人的声音，其他人说「小音」会被忽略（sherpa-onnx CAM++ 中文声纹模型，本地推理）

## 快速开始

### 1. 安装依赖
```bash
# 可选但推荐：先建虚拟环境
python -m venv .venv
.venv\Scripts\activate          # Windows；macOS/Linux 用 source .venv/bin/activate

pip install -r requirements.txt
```

### 2. 配置 API Key
复制 `.env.example` 为 `.env`，填入 DeepSeek Key：
```
DEEPSEEK_API_KEY=sk-你的key
```

### 3. 启动 Web 界面
```bash
python app.py
# 浏览器打开 http://127.0.0.1:7860
```

首次运行会自动下载 Whisper 模型（base 约 150MB）。默认走 **hf-mirror.com 国内镜像**（直连 huggingface.co 在国内会超时）——想用官方源就在 `.env` 里设 `HF_ENDPOINT=https://huggingface.co`。
端口可用环境变量覆盖：`PORT=7861 python app.py`。

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

### 声纹锁定（只认主人的声音）

默认开启（`SPEAKER_LOCK=1`）：

- **录入**：连接后第一次说「小音」时，自动从唤醒词音频提取声纹存下
- **校验**：之后每次说「小音」都会比对声纹——是主人才唤醒；不是则忽略（客户端提示「🔇 声音不是主人」）
- **重新录入**：按 `r` 重置对话或重新连接后，下一次唤醒重新录入
- 灵敏度用 `.env` 的 `SPEAKER_THRESHOLD` 调整（默认 0.6，误拒绝多就调低到 0.5）
- 模型：`models/speech_campplus_sv_zh_en_16k-common_advanced.onnx`（28MB，已随仓库提供）；`SPEAKER_LOCK=0` 可关闭

### 桌面宠物（Live2D）

让「小音」以桌面宠物形态常驻，随时说「小音」唤醒（需要 WebView2 运行时，Windows 11 自带）：

```bash
python pet.py                      # 透明无边框置顶窗口，默认屏幕右下角（不抢焦点）
python pet.py --host 192.168.1.8   # 连远程服务器
python pet.py --scale 1.2          # 缩放窗口
python pet.py --no-on-top          # 不置顶
```

**一键启动 / 开机自启 / 托盘驻留**：

- `双击 start_pet.bat`：自动检查并启动服务器（最小化窗口）+ 启动宠物（无控制台窗口）
- `双击 install_autostart.bat`：设置**开机自启动**（登录 Windows 自动拉起服务器+宠物）；`uninstall_autostart.bat` 取消
- 命令行等价写法：`python pet.py --install-autostart` / `--uninstall-autostart`
- **系统托盘**（默认开启，`--no-tray` 关闭）：托盘小图标常驻——双击显示/隐藏宠物、换模型、开机自启开关（带勾选状态）、退出

**三个模型**（右键「换模型」循环切换，选择会记住）：仙狐精灵 Senko / 黑猫精灵 Hijiki / Pio 小精灵。

| 操作 | 效果 |
|------|------|
| 说「小音」 | 唤醒并直接说话（受声纹锁定保护） |
| **单击宠物** | 免唤醒词直接聆听 |
| **双击宠物** | 摸头卖萌（官方触摸动作 + 爱心粒子） |
| 按住猫身拖动 | 移动位置（拖动时露出惊讶表情） |
| 滚轮 | 缩放（0.5~3 倍，自动记住） |
| 右键宠物 | 菜单：换模型 / 置顶 / 摸头 / 缩放 / 停止播报 / 退出（4 秒自动关闭） |
| 播报中说话 | 打断并继续听新问题（全双工） |

**自动散步**（黑猫专属）：90 秒无互动后，黑猫切换为透明像素小猫在桌面上来回踱步，任何互动立即回家。

动画状态：待机呼吸眨眼（瞳孔跟随鼠标）→ 聆听竖耳 → 思考歪头翻眼 → 说话嘴巴开合（气泡显示内容）→ 他人唤醒摇头拒绝 → 服务器断开时打盹变暗。

⚠️ 宠物与 `client.py` 共用麦克风，**不要同时运行**；网页开着「免提唤醒」时建议关闭，避免回复双重播报。

模型授权：Senko/Pio 来自开源模型合集仓库（hacxy/l2d-models，仅供学习参考，版权归原作者），Hijiki 为 Live2D 官方示例模型，像素猫来自 OpenGameArt（CC0）。

### 桌面 / 其他设备（CLI 客户端）

服务化后任意设备都可接入（服务器跑在 `python app.py` 的机器上）：

```bash
python client.py                      # 本机接入
python client.py --host 192.168.1.8   # 局域网其他机器接入（防火墙放行 TCP 7860）
python client.py --voice zh-CN-YunxiNeural  # 指定音色
```

按键：`r` 重置对话，`q` 退出；播报中直接说话即可打断。

### 手机使用（HTTPS + 可加到主屏幕）

手机浏览器**只在 HTTPS 下才给网页麦克风权限**（`http://192.168.x.x` 上 `navigator.mediaDevices`
是 undefined），所以手机端要另走一个 HTTPS 端口。**电脑端完全不受影响**——宠物和 CLI 走的是
`ws://`，绝不能把主服务改成 HTTPS，所以手机入口是单独一个 TLS 反向代理：

```bash
python make_cert.py       # 首次：生成带局域网 IP 的自签证书（零依赖，调系统 openssl）
python app.py             # 电脑端服务（照旧 HTTP 7860，宠物/CLI/浏览器都不变）
python phone_server.py    # 手机入口（HTTPS 7861，把流量转给 7860）
```

放行防火墙（管理员权限执行**一次**）：

```
netsh advfirewall firewall add rule name="小音助手 7861" dir=in action=allow protocol=TCP localport=7861
```

手机连同一个 WiFi，浏览器打开 `https://<电脑局域网IP>:7861`，然后：

1. 首次会提示证书不受信任（自签证书的正常现象）→ 无视警告继续
2. 装证书：访问 `https://<IP>:7861/wake-static/cert.pem`
   - Android：设置 → 安全 → 加密与凭据 → 安装证书 → CA 证书
   - iPhone：设置 → 通用 → VPN与设备管理 安装，再到 设置 → 通用 → 关于本机 → **证书信任设置**里开启信任
3. 重新打开页面 → 点「🎙️ 免提唤醒开关」→ 说「小音」
4. 想当 App 用：浏览器菜单选「添加到主屏幕」（Gradio 已生成 PWA manifest，图标是 `web/icon.png`）

**已知限制**（浏览器层面的，绕不过去）：

- **锁屏/切后台唤不醒**：手机锁屏后浏览器会挂起音频与 WebSocket，回到前台才恢复（代码里已处理恢复时的重连）
- **自签证书要手动信任**：iPhone 必须在「证书信任设置」里手动开启，否则 `wss://` 会被直接拒绝；
  自签若在你的手机上仍被拦，退路是用 [mkcert](https://github.com/FiloSottile/mkcert) 签一个受信任的本地 CA
- **安全**：放行防火墙后同网段其他设备也能访问这个服务（它带着 DeepSeek Key 和聊天记录），
  建议只在家里网络这么用；不想用了执行
  `netsh advfirewall firewall delete rule name="小音助手 7861"`
- 换了网络/IP 变了要重跑 `make_cert.py`（证书里写死了 IP）

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
| `{"type":"listen"}` | 显式动作免唤醒词聆听（仅待机生效；单击宠物/按钮使用，不经声纹校验） |
| `{"type":"stop"}` | 停止当前播报 |
| `{"type":"reset"}` | 重置对话上下文 |

**服务器 → 客户端**
| 事件 | 说明 |
|------|------|
| `{"type":"ready","sample_rate":16000,"chunk":1600,"kws":true}` | 连接建立 |
| `{"type":"wake","keyword":"小音","source":"kws"/"click"}` | 检测到唤醒词或显式点击（客户端本地响提示音），每连接 5s 冷却 |
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
| 🔧 工具调用 | Function Calling | 天气/空气质量（Open-Meteo）/ 时间 / 农历节日 / 计算器 / 汇率 / 新闻快讯 / 日程提醒 / 备忘录 / 快递（可选）/ QQ 音乐点歌与控制 |
| 💾 对话记忆 | 本地 JSON 持久化 | 重启恢复上下文，重置即清空 |
| ⏰ 日程提醒 | 本地 JSON（data/reminders.json） | 到点后在下一轮对话主动提醒，可查可删 |
| 🔊 语音合成 | Edge-TTS | 免费，中文自然度高 |
| 🖥️ 前端界面 | Gradio | Web UI，支持麦克风输入 |

### 快递查询（可选配置）

快递工具使用**快递鸟**免费接口（其余工具全部零账号、零 Key）：

1. 到 https://www.kdniao.com 免费注册，申请「即时查询」接口（RequestType 1002）
2. 在 `.env` 填写 `KDNIAO_EBUSINESS_ID`（商户ID）和 `KDNIAO_APP_KEY`（接口Key）
3. 之后说「查一下顺丰 SF1234567890」即可；未配置时小音会提示，不影响其他功能

### QQ 音乐（可选，Windows 本机）

本机装有 QQ 音乐客户端时，说「放首晴天」「下一首」「暂停」即可点歌和控制播放，**零配置**（自动查注册表定位客户端；装在非常规位置时在 `.env` 里填 `QQMUSIC_PATH`）。

实现上分两条路，可靠性不同：

- **播放控制**（暂停/继续/上一首/下一首）走客户端注册的 COM 接口 `QQMusicSvr.QQMusicPlayer`，纯 ctypes 调用，不需要窗口、不抢焦点，客户端缩在托盘里也能用。
- **点歌**走界面自动化：清空搜索框 → 打歌名 → 点联想列表第一条（实测**按回车只会跳到搜索结果页，点联想某一条才会播**）。主窗口被收进托盘时用 `SetWindowPlacement` 连位置一起写回拉回来；实在拉不回来会提示你点一下托盘图标。控件位置都按窗口尺寸取比例写死在 `music.py` 顶部常量里，客户端大版本更新后若布局变了需要重新校准。

已知限制：会员/无版权歌曲客户端自己会提示；点歌会短暂抢一次前台焦点（要往搜索框里输入）。

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
├── tools.py            # Function Calling 工具（天气/时间/计算器/汇率/备忘录…）
├── music.py            # QQ 音乐控制（COM 播放控制 + 界面自动化点歌）
├── audio_utils.py      # 音频工具（录音/播放/打断）
├── wakeword.py         # 唤醒词检测（sherpa-onnx KWS / ASR 兜底）
├── voice_server.py     # 全双工语音会话服务端（/ws/assistant 协议 + 会话状态机）
├── client.py           # CLI 薄客户端（任意设备接入服务器对话）
├── pet.py              # 桌面宠物（pywebview 透明窗口 + Live2D 三模型 + 像素散步 + 同一协议）
├── ws_audio.py         # 客户端共享音频组件（client.py 与 pet.py 共用）
├── web/wake_mode.js    # 浏览器端全双工会话脚本（推流/播放/打断）
├── web/pet/            # 宠物前端（双运行时页面 + Live2D 模型 + 像素散步素材）
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
| SILENCE_DURATION | 0.8 | 静音判定秒数（越大越等得久） |
| PORT | 7860 | Web 界面端口 |
| HF_ENDPOINT | https://hf-mirror.com | Whisper 模型下载源（国内默认镜像） |
| WAKE_WORD_ENABLED | 1 | 免提唤醒开关 |
| KWS_KEYWORDS_THRESHOLD | 0.25 | 唤醒词检测阈值（越小越灵敏） |
| SPEAKER_LOCK | 1 | 声纹锁定开关 |
| SPEAKER_THRESHOLD | 0.6 | 声纹匹配阈值（误拒绝多就调低） |

## 测试

不用 pytest，每个文件都是能直接跑的脚本（`python test_xxx.py`，末尾打印「全部通过 ✅」）：

| 脚本 | 覆盖内容 | 联网 |
|---|---|---|
| `test_stt_accuracy.py` | **识别准确率**：TTS 合成典型句子 → 喂 Whisper → 比对原文；断言平均命中率、不许退回繁体 | 要（edge-tts） |
| `test_voice_ws.py` | WebSocket 全协议（唤醒/打断/声纹/停止）+ 真实全链路 | 部分 |
| `test_voice_server.py` | 帧切分、VAD 采集、打断检测、队列折叠 | 否 |
| `test_streaming.py` | 流式 TTS 与打断（不联网不发声） | 否 |
| `test_function_calling.py` | 工具单元测试 + 真实 DeepSeek 集成 | 部分 |
| `test_wakeword.py` / `test_speaker.py` | 唤醒词匹配 / 声纹锁定（真实模型） | 否 |
| `test_pet.py` | 桌宠状态映射、UI 桥、静态服务、配置 | 否 |
| `test_music.py` | QQ 音乐定位、搜索解析、COM 接口、降级文案 | 部分 |

改识别相关代码（提示词、模型档位、音频处理）后建议跑一遍 `test_stt_accuracy.py` ——
准确率退化是单测覆盖不到的，只有真实音频能暴露。

## 后续扩展方向

- [x] 更多工具（快递查询、新闻快讯、日程提醒、空气质量）
- [x] Web 端全双工打断（免提唤醒，无需点击按钮）
- [x] WebSocket 服务化架构（/ws/assistant 统一协议 + 浏览器/CLI 双客户端）
- [x] 桌面宠物客户端（Live2D 三模型、免提唤醒、单击即听、摸头互动、自动散步）
- [x] 更多工具（农历节日、备忘录；股票基金等后续可加）
- [x] QQ 音乐点歌与播放控制（COM 接口 + 界面自动化，零依赖）
- [ ] 端侧部署（whisper.cpp + 嵌入式设备）

## License

MIT
