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

    # 预加载 Whisper 模型（首次需要下载）
    stt.load()

    print("\n[就绪] 开始对话吧！")
    print("  [Enter] 开始说话  [r] 重置对话  [q] 退出\n")

    # 主循环
    loop = asyncio.get_running_loop()
    while True:
        try:
            cmd = await loop.run_in_executor(None, input, ">>> ")
            cmd = cmd.strip().lower()

            if cmd == "q":
                print("再见！")
                break
            elif cmd == "r":
                bot.reset()
                print("[对话已重置]")
                continue
            elif cmd == "":
                await process_turn(recorder, stt, bot, tts, player)
            else:
                print("按 Enter 说话，按 r 重置，按 q 退出")

        except KeyboardInterrupt:
            print("\n再见！")
            break
        except Exception as e:
            print(f"\n[错误] {e}")
            continue


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
