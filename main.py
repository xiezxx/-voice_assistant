"""AI 语音助手主程序。

流程：用户说话 → Whisper 识别 → DeepSeek 理解回答 → Edge-TTS 朗读。

使用方法：
    python main.py            # 交互模式（按 Enter 开始说话）
    python main.py --devices  # 列出音频设备
    python main.py --voices   # 列出可用音色
"""

import asyncio
import sys
import os
import time
from pathlib import Path

from config import Config
from audio_utils import AudioRecorder, AudioPlayer, list_devices
from stt import SpeechRecognizer
from llm import ChatBot
from tts import SpeechSynthesizer
from speech_utils import sentence_stream
from conversation_store import save_conversation, load_conversation, clear_conversation
from wakeword import create_wake_listener


async def _wait_for_trigger(loop, wake: WakeWordListener | None) -> str:
    """等待一轮触发。

    唤醒词模式下：轮询麦克风检测「小音，小音」，同时支持键盘按键；
    普通模式下：等待回车。
    """
    if wake is None:
        return (await loop.run_in_executor(None, input, ">>> ")).strip().lower()

    print("💤 待机中：说「小音，小音」唤醒；按 r 重置、q 退出", flush=True)
    try:
        import msvcrt  # Windows 键盘轮询
    except ImportError:
        msvcrt = None
    while True:
        if wake.wait_for_wake_word(timeout=0.2):
            print("\n🔔 检测到唤醒词「小音，小音」", flush=True)
            return ""
        if msvcrt is not None and msvcrt.kbhit():
            ch = msvcrt.getch().decode("utf-8", errors="ignore").strip().lower()
            if ch in ("\r", "\n"):
                ch = ""
            print(f"\n>>> {ch}", flush=True)
            return ch
        await asyncio.sleep(0)


async def process_turn(
    recorder: AudioRecorder,
    stt: SpeechRecognizer,
    bot: ChatBot,
    tts: SpeechSynthesizer,
    player: AudioPlayer,
):
    """处理一轮对话：听 → 想 → 说（流式：按句生成、边生成边播报，支持语音打断）。"""

    # 1. 录音
    audio = recorder.record_until_silence(
        threshold=Config.VAD_THRESHOLD,
        silence_duration=Config.SILENCE_DURATION,
    )
    if audio is None:
        return

    # 2. 语音识别
    print("[STT] 识别中...", end="", flush=True)
    t_start = time.time()
    user_text = stt.transcribe(audio)
    t_stt = time.time() - t_start

    if not user_text:
        print("\r[STT] 未能识别到有效文字")
        return

    print(f"\r你: {user_text}")

    # 3. LLM 流式思考 + 按句合成播报（播报时监听麦克风，用户一开口立即停止）
    print("[LLM] 思考中...", end="", flush=True)
    t_start = time.time()
    parts: list[str] = []
    interrupted = False
    try:
        async for sentence in sentence_stream(bot.chat_stream(user_text)):
            if bot.status:
                print(f"\r{bot.status}", end="", flush=True)
            parts.append(sentence)
            if len(parts) == 1:
                print(f"\rAI: {sentence}", end="", flush=True)
            else:
                print(sentence, end="", flush=True)

            audio_path = await tts.synthesize(sentence)
            try:
                if player.play_file_with_barge_in(
                    audio_path,
                    threshold=Config.VAD_THRESHOLD,
                    input_device=recorder.device,
                ):
                    interrupted = True
                    break
            finally:
                try:
                    os.unlink(audio_path)
                except OSError:
                    pass
    except RuntimeError as e:
        print(f"\n[错误] {e}")
        return
    t_llm = time.time() - t_start

    full_reply = "".join(parts)
    if interrupted:
        # 打断后手动补录助手回复，保持上下文完整
        bot.conversation.append({"role": "assistant", "content": full_reply})
        print("\n[打断] 检测到用户说话，已停止播报")
    print(f"\n      (STT: {t_stt:.1f}s | LLM: {t_llm:.1f}s)")


async def main():
    print("=" * 50)
    print("  AI 语音助手 v1.0")
    print("  支持：Whisper(听) + DeepSeek(想) + Edge-TTS(说)")
    print("=" * 50)

    if not Config.validate():
        return

    # 初始化各模块
    print("\n[初始化] 加载各模块...")

    stt = SpeechRecognizer()
    recorder = AudioRecorder()
    bot = ChatBot()
    tts = SpeechSynthesizer()
    player = AudioPlayer()

    # 恢复上次对话上下文
    _, saved_conversation = load_conversation()
    if saved_conversation:
        bot.conversation = saved_conversation
        print("[记忆] 已恢复上次对话上下文")

    # 唤醒词（默认开启：Picovoice 配置齐时低延迟，否则用 ASR 关键词方案）
    wake = None
    if Config.WAKE_WORD_ENABLED:
        wake, mode = create_wake_listener(stt)
        if mode == "porcupine":
            print("[唤醒词] Porcupine 模式已开启")
        else:
            print("[唤醒词] ASR 模式已开启（说「小音」唤醒，响应约1秒）")

    # 预加载 Whisper 模型（首次需要下载）
    stt.load()

    print("\n[就绪] 开始对话吧！")
    if wake is None:
        print("  [Enter] 开始说话  [r] 重置对话  [q] 退出\n")

    # 主循环
    loop = asyncio.get_running_loop()
    try:
        while True:
            try:
                cmd = await _wait_for_trigger(loop, wake)

                if cmd == "q":
                    save_conversation([], bot.conversation)
                    print("再见！（对话上下文已保存）")
                    break
                elif cmd == "r":
                    bot.reset()
                    clear_conversation()
                    print("[对话已重置]")
                    continue
                elif cmd == "":
                    if wake is not None:
                        wake.close()  # 释放麦克风给对话录音，待机时自动重开
                    await process_turn(recorder, stt, bot, tts, player)
                    save_conversation([], bot.conversation)
                else:
                    print("按 Enter 说话，按 r 重置，按 q 退出")

            except KeyboardInterrupt:
                print("\n再见！")
                break
            except Exception as e:
                print(f"\n[错误] {e}")
                continue
    finally:
        if wake is not None:
            wake.close()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        if sys.argv[1] == "--devices":
            list_devices()
        elif sys.argv[1] == "--voices":
            SpeechSynthesizer.list_voices()
        elif sys.argv[1] == "--help":
            print("AI 语音助手 v1.0")
            print("  python main.py           启动语音助手")
            print("  python main.py --devices 列出音频设备")
            print("  python main.py --voices  列出可用 TTS 音色")
    else:
        asyncio.run(main())
