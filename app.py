"""AI 语音助手 — Gradio Web 界面。

支持语音输入（麦克风）和文字输入两种交互方式。
流程：语音 → Whisper 识别 → DeepSeek 对话 → Edge-TTS 朗读。

本地运行：
    python app.py
    浏览器打开 http://127.0.0.1:7860
"""

import asyncio
import os
import sys

# ⚠️ 必须在 import gradio 之前设置，否则代理会把 localhost 请求转发到代理服务器导致 502
for key in ("NO_PROXY", "no_proxy"):
    os.environ[key] = "localhost,127.0.0.1,::1"

import time

import gradio as gr
import numpy as np

from config import Config
from stt import SpeechRecognizer
from llm import ChatBot
from tts import SpeechSynthesizer
from speech_utils import sentence_stream, audio_duration_sec

# ── 全局初始化 ──────────────────────────────────────────────

print("[初始化] 加载 Whisper 模型...")
stt = SpeechRecognizer()
stt.load()

bot = ChatBot()
tts = SpeechSynthesizer()

# 可选音色列表
VOICE_CHOICES = [
    ("小伊 - 女声 (推荐)", "zh-CN-XiaoyiNeural"),
    ("晓晓 - 女声", "zh-CN-XiaoxiaoNeural"),
    ("云希 - 男声", "zh-CN-YunxiNeural"),
    ("云健 - 男声", "zh-CN-YunjianNeural"),
    ("晓悠 - 女童声", "zh-CN-XiaoyouNeural"),
]
VOICE_NAMES = [v[1] for v in VOICE_CHOICES]


# ── 核心处理 ────────────────────────────────────────────────

async def process_voice(audio: tuple, history: list, voice: str):
    """处理麦克风语音输入（识别 → LLM 流式生成 → 按句合成逐句播报）。

    Args:
        audio: Gradio Audio 组件返回的 (sample_rate, np.ndarray)
        history: 对话历史 [{"role", "content"}, ...]
        voice: TTS 音色 ShortName
    """
    if audio is None:
        yield history, "⚠️ 未检测到音频输入", None
        return

    sample_rate, audio_data = audio

    # 1. 语音识别
    t0 = time.time()
    user_text = stt.transcribe(audio_data, sample_rate=sample_rate)
    t_stt = time.time() - t0

    if not user_text:
        yield history, f"⚠️ 未识别到有效文字 (STT: {t_stt:.1f}s)", None
        return

    # 2. LLM 流式对话 + 按句合成播报
    history.append({"role": "user", "content": f"🎤 {user_text}"})
    history.append({"role": "assistant", "content": ""})

    tts.VOICE = voice
    t0 = time.time()
    parts: list[str] = []
    prev_end = time.time()  # 上一句音频预计播完的时刻（用于控制播报节奏）
    try:
        async for sentence in sentence_stream(bot.chat_stream(user_text)):
            parts.append(sentence)
            history[-1]["content"] = "🤖 " + "".join(parts)
            yield history, bot.status or "⏳ 正在生成...", None

            # 合成当前句（失败则跳过播报，文字已显示）
            try:
                audio_path = await tts.synthesize(sentence)
            except Exception:
                continue

            # 等上一句播完再播这一句，避免浏览器里互相打断
            now = time.time()
            if now < prev_end:
                await asyncio.sleep(prev_end - now)
            yield history, "🔊 正在播报...", audio_path
            prev_end = time.time() + audio_duration_sec(audio_path, len(sentence))
    except Exception as e:
        history[-1]["content"] = f"⚠️ LLM 调用失败：{e}"
        yield history, "⚠️ LLM 调用失败", None
        return
    t_llm = time.time() - t0

    full_reply = "".join(parts)
    if not full_reply:
        history[-1]["content"] = "🤖 （暂无回复）"
        yield history, "⚠️ 模型返回空回复", None
        return

    status = f"✅ STT: {t_stt:.1f}s | LLM: {t_llm:.1f}s | 已完成播报"
    yield history, status, None


async def process_text(text: str, history: list, voice: str):
    """处理文字输入（LLM 流式生成 → 按句合成逐句播报）。"""
    if not text or not text.strip():
        yield history, "⚠️ 请输入文字", None
        return

    user_text = text.strip()

    history.append({"role": "user", "content": f"⌨️ {user_text}"})
    history.append({"role": "assistant", "content": ""})

    tts.VOICE = voice
    t0 = time.time()
    parts: list[str] = []
    prev_end = time.time()
    try:
        async for sentence in sentence_stream(bot.chat_stream(user_text)):
            parts.append(sentence)
            history[-1]["content"] = "🤖 " + "".join(parts)
            yield history, bot.status or "⏳ 正在生成...", None

            try:
                audio_path = await tts.synthesize(sentence)
            except Exception:
                continue

            now = time.time()
            if now < prev_end:
                await asyncio.sleep(prev_end - now)
            yield history, "🔊 正在播报...", audio_path
            prev_end = time.time() + audio_duration_sec(audio_path, len(sentence))
    except Exception as e:
        history[-1]["content"] = f"⚠️ LLM 调用失败：{e}"
        yield history, "⚠️ LLM 调用失败", None
        return
    t_llm = time.time() - t0

    full_reply = "".join(parts)
    if not full_reply:
        history[-1]["content"] = "🤖 （暂无回复）"
        yield history, "⚠️ 模型返回空回复", None
        return

    status = f"✅ LLM: {t_llm:.1f}s | 已完成播报"
    yield history, status, None


def reset_conversation():
    """重置对话上下文。"""
    bot.reset()
    return [], "🔄 对话已重置", None


# ── UI 布局 ──────────────────────────────────────────────────

THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="slate",
    font=gr.themes.GoogleFont("Inter"),
)

CSS = """
.gradio-container { max-width: 960px !important; margin: auto !important; }
.status-box textarea { font-family: 'Consolas', monospace; font-size: 13px; }
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

    # 状态
    chat_state = gr.State([])

    with gr.Row():
        # ── 左侧：输入区 ──
        with gr.Column(scale=1):
            gr.Markdown("### 📥 输入")

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
                    value="zh-CN-XiaoyiNeural",
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

    # ── 事件绑定 ──

    # 语音输入：录音结束后自动处理
    audio_input.stop_recording(
        fn=process_voice,
        inputs=[audio_input, chat_state, voice_dropdown],
        outputs=[chatbot, status, audio_output],
    )

    # 文字输入
    text_btn.click(
        fn=process_text,
        inputs=[text_input, chat_state, voice_dropdown],
        outputs=[chatbot, status, audio_output],
    ).then(lambda: "", None, text_input)  # 清空输入框

    # 重置
    reset_btn.click(
        fn=reset_conversation,
        inputs=[],
        outputs=[chatbot, status, audio_output],
    )

    # 停止播报：暂停浏览器中所有 audio 元素（纯前端 JS）
    stop_btn.click(
        None,
        js="() => { document.querySelectorAll('audio').forEach(a => { a.pause(); a.currentTime = 0; }); }",
    )

    # 同步 chat_state 与 chatbot
    def sync_state(history):
        return history

    chatbot.change(fn=sync_state, inputs=[chatbot], outputs=[chat_state])

    # ── 页脚 ──
    gr.Markdown(
        """
        ---
        💡 **提示**：点击麦克风按钮，说话后再次点击停止录音，AI 会自动回复。
        建议使用 Chrome/Edge 浏览器以获得最佳麦克风体验。
        """
    )


if __name__ == "__main__":
    print("\n[就绪] 启动 Web 界面...")
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,  # 本地运行不需要公网链接
        theme=THEME,
        css=CSS,
    )
