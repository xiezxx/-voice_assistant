"""AI 语音助手 — Gradio Web 界面。

支持语音输入（麦克风）和文字输入两种交互方式，以及免提唤醒（说「小音」）。
流程：语音 → Whisper 识别 → DeepSeek 对话 → Edge-TTS 朗读。

本地运行：
    python app.py
    浏览器打开 http://127.0.0.1:7860

免提唤醒：点击「🎙️ 免提唤醒开关」授权麦克风后持续监听，说「小音」即唤醒；
浏览器麦克风音频经 WebSocket 流回服务器，用 sherpa-onnx KWS 本地模型毫秒级检测。
"""

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# ⚠️ 必须在 import gradio 之前设置，否则代理会把 localhost 请求转发到代理服务器导致 502
for key in ("NO_PROXY", "no_proxy"):
    os.environ[key] = "localhost,127.0.0.1,::1"

import time

import gradio as gr
import numpy as np
from fastapi.staticfiles import StaticFiles

from config import Config
from stt import SpeechRecognizer
from llm import ChatBot
from tts import SpeechSynthesizer
from speech_utils import sentence_stream, audio_duration_sec
from conversation_store import save_conversation, load_conversation, clear_conversation
from wakeword import KwsFeedDetector, sherpa_available
from wake_server import (
    WakeDeps,
    register_routes,
    pop_latest_turn,
    should_advance,
    unlink_path,
)

# ── 全局初始化 ──────────────────────────────────────────────

print("[初始化] 加载 Whisper 模型...")
stt = SpeechRecognizer()
stt.load()

bot = ChatBot()
tts = SpeechSynthesizer()

# 恢复上次会话（界面历史 + API 上下文）
_loaded_history, _loaded_conversation = load_conversation()
bot.conversation = _loaded_conversation

# 可选音色列表
VOICE_CHOICES = [
    ("小伊 - 女声 (推荐)", "zh-CN-XiaoyiNeural"),
    ("晓晓 - 女声", "zh-CN-XiaoxiaoNeural"),
    ("云希 - 男声", "zh-CN-YunxiNeural"),
    ("云健 - 男声", "zh-CN-YunjianNeural"),
    ("晓悠 - 女童声", "zh-CN-XiaoyouNeural"),
]
VOICE_NAMES = [v[1] for v in VOICE_CHOICES]


# ── 服务端会话状态（单一事实源） ──────────────────────────────

@dataclass
class PlaybackState:
    """免提轮次的逐句播报状态（由 gr.Timer 消费）。"""

    turn: Optional[list] = None    # 当前轮句子 [{text, path}]
    idx: int = 0
    ready_at: float = 0.0          # 上一句播完的时刻，之后才可播下一句
    final_status: str = ""
    last_history: Optional[list] = None
    done: bool = False


SERVER_HISTORY: list = _loaded_history   # 对话历史（含 🎤/🤖 表情前缀），所有路径共用
SERVER_VOICE = "zh-CN-XiaoyiNeural"      # 音色由 dropdown.change 同步
TURN_LOCK = asyncio.Lock()   # 串行化所有轮次（手动/免提共用，防 LLM 上下文并发破坏）
STT_LOCK = asyncio.Lock()    # faster-whisper 未证明线程安全 → 串行化
KWS_LOCK = asyncio.Lock()    # sherpa decode 串行化（跨 WS 连接）
WAKE_QUEUE: asyncio.Queue = asyncio.Queue()
PLAYBACK = PlaybackState()
PLAYBACK_PAUSED = False      # 停止播报按钮置位；新轮次复位

# KWS 可用性在启动时算好（按钮交互状态 + 检测器创建共用）
_kws_ok, _kws_reason = sherpa_available()
if not _kws_ok:
    print(f"[免提唤醒] KWS 不可用（{_kws_reason}），网页免提唤醒按钮将禁用")


# ── 核心处理 ────────────────────────────────────────────────

async def process_voice(audio: tuple):
    """处理麦克风语音输入（识别 → LLM 流式生成 → 按句合成逐句播报）。

    Args:
        audio: Gradio Audio 组件返回的 (sample_rate, np.ndarray)
    """
    if audio is None:
        yield SERVER_HISTORY, "⚠️ 未检测到音频输入", None
        return

    sample_rate, audio_data = audio

    # 1. 语音识别（阻塞 CPU 调用移出事件循环，锁内串行化）
    t0 = time.time()
    async with TURN_LOCK:
        async with STT_LOCK:
            user_text = await asyncio.to_thread(stt.transcribe, audio_data, sample_rate)
        t_stt = time.time() - t0

        if not user_text:
            yield SERVER_HISTORY, f"⚠️ 未识别到有效文字 (STT: {t_stt:.1f}s)", None
            return

        # 2. LLM 流式对话 + 按句合成播报
        SERVER_HISTORY.append({"role": "user", "content": f"🎤 {user_text}"})
        SERVER_HISTORY.append({"role": "assistant", "content": ""})

        tts.VOICE = SERVER_VOICE
        t0 = time.time()
        parts: list[str] = []
        prev_end = time.time()  # 上一句音频预计播完的时刻（用于控制播报节奏）
        try:
            async for sentence in sentence_stream(bot.chat_stream(user_text)):
                parts.append(sentence)
                SERVER_HISTORY[-1]["content"] = "🤖 " + "".join(parts)
                yield SERVER_HISTORY, bot.status or "⏳ 正在生成...", None

                # 合成当前句（失败则跳过播报，文字已显示）
                try:
                    audio_path = await tts.synthesize(sentence)
                except Exception:
                    continue

                # 等上一句播完再播这一句，避免浏览器里互相打断
                now = time.time()
                if now < prev_end:
                    await asyncio.sleep(prev_end - now)
                yield SERVER_HISTORY, "🔊 正在播报...", audio_path
                prev_end = time.time() + audio_duration_sec(audio_path, len(sentence))
        except Exception as e:
            SERVER_HISTORY[-1]["content"] = f"⚠️ LLM 调用失败：{e}"
            save_conversation(SERVER_HISTORY, bot.conversation)
            yield SERVER_HISTORY, "⚠️ LLM 调用失败", None
            return
        t_llm = time.time() - t0

        full_reply = "".join(parts)
        if not full_reply:
            SERVER_HISTORY[-1]["content"] = "🤖 （暂无回复）"
            save_conversation(SERVER_HISTORY, bot.conversation)
            yield SERVER_HISTORY, "⚠️ 模型返回空回复", None
            return

        status = f"✅ STT: {t_stt:.1f}s | LLM: {t_llm:.1f}s | 已完成播报"
        yield SERVER_HISTORY, status, None
        save_conversation(SERVER_HISTORY, bot.conversation)


async def process_text(text: str):
    """处理文字输入（LLM 流式生成 → 按句合成逐句播报）。"""
    if not text or not text.strip():
        yield SERVER_HISTORY, "⚠️ 请输入文字", None
        return

    user_text = text.strip()

    async with TURN_LOCK:
        SERVER_HISTORY.append({"role": "user", "content": f"⌨️ {user_text}"})
        SERVER_HISTORY.append({"role": "assistant", "content": ""})

        tts.VOICE = SERVER_VOICE
        t0 = time.time()
        parts: list[str] = []
        prev_end = time.time()
        try:
            async for sentence in sentence_stream(bot.chat_stream(user_text)):
                parts.append(sentence)
                SERVER_HISTORY[-1]["content"] = "🤖 " + "".join(parts)
                yield SERVER_HISTORY, bot.status or "⏳ 正在生成...", None

                try:
                    audio_path = await tts.synthesize(sentence)
                except Exception:
                    continue

                now = time.time()
                if now < prev_end:
                    await asyncio.sleep(prev_end - now)
                yield SERVER_HISTORY, "🔊 正在播报...", audio_path
                prev_end = time.time() + audio_duration_sec(audio_path, len(sentence))
        except Exception as e:
            SERVER_HISTORY[-1]["content"] = f"⚠️ LLM 调用失败：{e}"
            save_conversation(SERVER_HISTORY, bot.conversation)
            yield SERVER_HISTORY, "⚠️ LLM 调用失败", None
            return
        t_llm = time.time() - t0

        full_reply = "".join(parts)
        if not full_reply:
            SERVER_HISTORY[-1]["content"] = "🤖 （暂无回复）"
            save_conversation(SERVER_HISTORY, bot.conversation)
            yield SERVER_HISTORY, "⚠️ 模型返回空回复", None
            return

        status = f"✅ LLM: {t_llm:.1f}s | 已完成播报"
        yield SERVER_HISTORY, status, None
        save_conversation(SERVER_HISTORY, bot.conversation)


async def wake_tick():
    """gr.Timer 轮询：消费免提唤醒队列 → 更新聊天/状态/逐句音频。

    免提轮次在自定义 HTTP 路由里处理（非 Gradio 事件上下文），Timer 是把它
    同步进 UI 的唯一正规途径。空队列时全部 gr.skip()，避免 0.5 秒一次的重绘。
    """
    global PLAYBACK_PAUSED

    latest = pop_latest_turn(WAKE_QUEUE)
    if latest is not None:
        if PLAYBACK.turn:  # 新轮替换旧轮（打断语义）：丢弃未播完的旧轮
            for s in PLAYBACK.turn[PLAYBACK.idx:]:
                unlink_path(s["path"])
        PLAYBACK.turn = latest["sentences"]
        PLAYBACK.idx = 0
        PLAYBACK.ready_at = 0.0
        PLAYBACK.final_status = latest["status"]
        PLAYBACK.last_history = latest["history"]
        PLAYBACK.done = False
        PLAYBACK_PAUSED = False
        chatbot_out = PLAYBACK.last_history
    else:
        chatbot_out = gr.skip()

    if PLAYBACK.turn is None or PLAYBACK.done:
        return chatbot_out, gr.skip(), gr.skip()

    now = time.time()
    if not should_advance(PLAYBACK.ready_at, now, PLAYBACK_PAUSED):
        return chatbot_out, gr.skip(), gr.skip()  # 上一句仍在播/已暂停：跳过本 tick

    if PLAYBACK.idx >= len(PLAYBACK.turn):
        # 全部播完：清理剩余临时文件，回报最终状态一次
        for s in PLAYBACK.turn:
            unlink_path(s["path"])
        PLAYBACK.turn = None
        PLAYBACK.done = True
        return chatbot_out, PLAYBACK.final_status, None

    if PLAYBACK.idx > 0:
        unlink_path(PLAYBACK.turn[PLAYBACK.idx - 1]["path"])  # 上一句已被浏览器取走并播完
    s = PLAYBACK.turn[PLAYBACK.idx]
    PLAYBACK.idx += 1
    PLAYBACK.ready_at = (
        now + await asyncio.to_thread(audio_duration_sec, s["path"], len(s["text"])) + 0.15
    )
    return chatbot_out, f"🔊 正在播报 ({PLAYBACK.idx}/{len(PLAYBACK.turn)})", s["path"]


async def reset_conversation():
    """重置对话上下文，并清除已保存的会话与免提队列。"""
    async with TURN_LOCK:
        bot.reset()
        clear_conversation()
        SERVER_HISTORY.clear()
        pop_latest_turn(WAKE_QUEUE)  # 丢弃排队轮次并清理 mp3
        if PLAYBACK.turn:
            for s in PLAYBACK.turn:
                unlink_path(s["path"])
            PLAYBACK.turn = None
        PLAYBACK.done = True
    return [], "🔄 对话已重置", None


def _set_voice(voice: str):
    """音色下拉框同步到服务器全局（免提轮次使用同一音色）。"""
    global SERVER_VOICE
    SERVER_VOICE = voice
    return gr.skip()


def _pause_playback():
    """停止播报按钮：暂停免提轮次的逐句播报（浏览器端 JS 同时静音）。"""
    global PLAYBACK_PAUSED
    PLAYBACK_PAUSED = True
    return gr.skip()


# ── UI 布局 ──────────────────────────────────────────────────

THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="slate",
    font=gr.themes.GoogleFont("Inter"),
)

CSS = """
.gradio-container { max-width: 960px !important; margin: auto !important; }
.status-box textarea { font-family: 'Consolas', monospace; font-size: 13px; }
#wake-status { font-size: 14px; padding: 8px 12px; border-radius: 8px; background: #f3f4f6; }
footer { display: none !important; }
"""

with gr.Blocks() as demo:
    gr.Markdown(
        """
        # 🎙️ AI 语音助手 — 小音

        基于 **Whisper**（听）+ **DeepSeek**（想）+ **Edge-TTS**（说）的智能语音助手。
        支持语音和文字两种输入方式，对话上下文保持。
        """
    )

    with gr.Row():
        # ── 左侧：输入区 ──
        with gr.Column(scale=1):
            gr.Markdown("### 📥 输入")

            wake_btn = gr.Button(
                "🎙️ 免提唤醒开关",
                variant="primary",
                size="sm",
                elem_id="wake-btn",
                interactive=_kws_ok,
            )
            wake_status = gr.HTML(
                "<span style='color:#888'>"
                + (
                    "免提唤醒未开启 — 点击上方按钮并允许麦克风，之后说「小音」即可免手动操作"
                    if _kws_ok
                    else f"免提唤醒不可用（{_kws_reason}）— 请用下方麦克风按钮"
                )
                + "</span>",
                elem_id="wake-status",
            )

            audio_input = gr.Audio(
                label="🎤 点击录音（说中文）",
                sources=["microphone"],
                type="numpy",
            )

            gr.Markdown("--- 或 ---")

            text_input = gr.Textbox(
                label="⌨️ 文字输入",
                placeholder="输入你想说的话...",
                lines=2,
            )
            text_btn = gr.Button("发送文字", variant="secondary", size="sm")

            with gr.Accordion("⚙️ 设置", open=False):
                voice_dropdown = gr.Dropdown(
                    choices=VOICE_NAMES,
                    value=SERVER_VOICE,
                    label="TTS 音色",
                    info="选择 AI 朗读的音色",
                )
                reset_btn = gr.Button("🔄 重置对话", variant="stop", size="sm")
                stop_btn = gr.Button("⏹ 停止播报", variant="stop", size="sm")

        # ── 右侧：输出区 ──
        with gr.Column(scale=1):
            gr.Markdown("### 📤 对话")

            chatbot = gr.Chatbot(
                label="对话记录",
                value=SERVER_HISTORY,
                height=400,
            )

            audio_output = gr.Audio(
                label="🔊 AI 语音回复",
                type="filepath",
                autoplay=True,
            )

    # ── 底部状态栏 ──
    status = gr.Textbox(
        label="状态",
        value="🟢 就绪 — 点击麦克风开始说话",
        interactive=False,
        elem_classes=["status-box"],
    )

    # ── 免提唤醒轮询（0.5s 消费一次队列，播放节奏由 audio_duration_sec 门控） ──
    wake_timer = gr.Timer(0.5)

    # ── 事件绑定 ──

    # 免提唤醒开关：纯前端逻辑（web/wake_mode.js 中的 window.wakeToggle）
    wake_btn.click(
        fn=None,
        js="() => { if (window.wakeToggle) window.wakeToggle();"
        " else alert('免提唤醒脚本未加载，请刷新页面'); }",
    )

    # 语音输入：录音结束后自动处理
    audio_input.stop_recording(
        fn=process_voice,
        inputs=[audio_input],
        outputs=[chatbot, status, audio_output],
    )

    # 文字输入
    text_btn.click(
        fn=process_text,
        inputs=[text_input],
        outputs=[chatbot, status, audio_output],
    ).then(lambda: "", None, text_input)  # 清空输入框

    # 重置
    reset_btn.click(
        fn=reset_conversation,
        inputs=[],
        outputs=[chatbot, status, audio_output],
    )

    # 停止播报：暂停浏览器中所有 audio 元素（纯前端 JS）+ 暂停免提逐句播报
    stop_btn.click(
        fn=_pause_playback,
        js="() => { document.querySelectorAll('audio').forEach(a => { a.pause(); a.currentTime = 0; }); }",
        outputs=[status],
    )

    # 音色同步到服务器全局（免提轮次使用同一音色）
    voice_dropdown.change(fn=_set_voice, inputs=[voice_dropdown], outputs=[status])

    # 免提唤醒队列 → UI
    wake_timer.tick(fn=wake_tick, outputs=[chatbot, status, audio_output])

    # ── 页脚 ──
    gr.Markdown(
        """
        ---
        💡 **提示**：点击麦克风按钮，说话后再次点击停止录音，AI 会自动回复。
        或者点击「🎙️ 免提唤醒开关」后直接说「小音」唤醒。
        建议使用 Chrome/Edge 浏览器以获得最佳麦克风体验。
        """
    )

    # 免提唤醒脚本通过 launch(head=...) 注入（gradio 6 的 head 参数在 launch 上，
    # 直接在 demo.head 赋值会被 launch 覆盖为 None）


# ── 自定义路由注册（必须在 launch 之前） ───────────────────────

_kws_detector = KwsFeedDetector() if _kws_ok else None
_wake_deps = WakeDeps(
    kws=_kws_detector,
    stt=stt,
    bot=bot,
    tts=tts,
    history=SERVER_HISTORY,
    get_voice=lambda: SERVER_VOICE,
    turn_lock=TURN_LOCK,
    stt_lock=STT_LOCK,
    kws_lock=KWS_LOCK,
    queue=WAKE_QUEUE,
)
register_routes(demo.app, _wake_deps)

# 注意：不能用 /static 前缀——gradio 自带 /static/{path:path} 捕获路由会先截走请求
demo.app.mount(
    "/wake-static",
    StaticFiles(directory=str(Path(__file__).parent / "web")),
    name="wake-static",
)


if __name__ == "__main__":
    print("\n[就绪] 启动 Web 界面...")
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,  # 本地运行不需要公网链接
        theme=THEME,
        css=CSS,
        head='<script defer src="/wake-static/wake_mode.js"></script>',
        _app=demo.app,  # 关键：复用 app，保留自定义路由（否则 launch 会重建导致路由丢失）
    )
