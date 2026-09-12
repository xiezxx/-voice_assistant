# -*- coding: utf-8 -*-
"""小音 CLI 客户端 —— 连接服务器 /ws/assistant 的全双工语音终端。

麦克风持续流式上行（含 AI 播报期间，供服务器检测说话打断），
播放服务器流式下发的 mp3 句子。任意局域网机器均可接入：

    python client.py                     # 本机（需先 python app.py 启动服务器）
    python client.py --host 192.168.1.8  # 局域网其他机器
    python client.py --voice zh-CN-XiaoyiNeural   # 指定音色（覆盖服务器全局）

按键：r 重置对话，q 退出；播报中直接说话即可打断（服务器检测）。
"""

import argparse
import asyncio
import json
import os
import sys
import tempfile

import numpy as np
import sounddevice as sd
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

SAMPLE_RATE = 16000
CHUNK = 1600


class PygamePlayer:
    """mp3 字节 → 临时文件 → 队列顺序播放；stop() 供打断时清空并静音。"""

    def __init__(self):
        import pygame

        pygame.mixer.pre_init(44100, -16, 2, 512)  # Windows 下 MP3 播放需提前设置
        pygame.mixer.init()
        self._q: asyncio.Queue = asyncio.Queue()

    def enqueue(self, mp3: bytes):
        f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        f.write(mp3)
        f.close()
        self._q.put_nowait(f.name)

    def stop(self):
        """打断：停止当前播放并清空队列（跨线程调用 SDL stop 广泛安全）。"""
        import pygame

        pygame.mixer.music.stop()
        while not self._q.empty():
            try:
                os.unlink(self._q.get_nowait())
            except OSError:
                pass

    def _play_blocking(self, path: str):
        import pygame

        try:
            pygame.mixer.music.load(path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
        finally:
            try:
                os.unlink(path)  # 播完再删（Windows 文件锁）
            except OSError:
                pass

    async def run(self):
        while True:
            path = await self._q.get()
            await asyncio.to_thread(self._play_blocking, path)


def make_mic_stream(loop, q: asyncio.Queue) -> sd.InputStream:
    """麦克风回调线程 → 事件循环队列（float32 → int16 bytes）。"""

    def cb(indata, frames, t, status):
        pcm = np.clip(np.rint(np.clip(indata, -1, 1) * 32768), -32768, 32767).astype("<i2")
        loop.call_soon_threadsafe(q.put_nowait, pcm.tobytes())

    return sd.InputStream(
        samplerate=SAMPLE_RATE, channels=1, dtype="float32", blocksize=CHUNK, callback=cb
    )


async def recv_loop(ws, player: PygamePlayer):
    async for msg in ws:
        if isinstance(msg, bytes):
            player.enqueue(msg)  # audio 事件后的二进制帧 = 完整 mp3
            continue
        try:
            ev = json.loads(msg)
        except ValueError:
            continue
        t = ev.get("type")
        if t == "wake":
            print("\n🔔 唤醒 — 请说话，说完停顿约 1 秒", flush=True)
        elif t == "transcript" and ev.get("text"):
            print(f"\n你: {ev['text']}", flush=True)
        elif t == "sentence":
            print(ev["text"], end="", flush=True)
        elif t == "status":
            text = ev.get("text", "")
            if text:
                print(f"\r[{text}]", end="", flush=True)
        elif t == "barge_in":
            player.stop()
            print("\n⏹ 被打断 — 请继续说", flush=True)
        elif t == "speaker_reject":
            print("\n🔇 声音不是主人，已忽略", flush=True)
        elif t == "turn_end":
            print(f"\n[状态] {ev.get('status', '')}", flush=True)
        elif t == "error":
            print(f"\n⚠️ {ev.get('error', '')}", flush=True)


async def keyboard_loop(ws, stop_ev: asyncio.Event):
    try:
        import msvcrt  # Windows 键盘轮询
    except ImportError:
        return  # 非 Windows：仅支持 Ctrl+C 退出
    while not stop_ev.is_set():
        await asyncio.sleep(0.2)
        if not msvcrt.kbhit():
            continue
        ch = msvcrt.getch().decode(errors="ignore").strip().lower()
        if ch == "q":
            stop_ev.set()
        elif ch == "r":
            await ws.send(json.dumps({"type": "reset"}))
            print("\n[对话已重置]", flush=True)


async def run(args):
    player = PygamePlayer()
    stop_ev = asyncio.Event()
    retry = 0
    while not stop_ev.is_set():
        try:
            async with connect(f"ws://{args.host}:{args.port}/ws/assistant") as ws:
                retry = 0
                print(f"✅ 已连接 {args.host}:{args.port} — 说「小音」唤醒（r 重置 / q 退出）", flush=True)
                await ws.send(json.dumps({"type": "hello", "voice": args.voice or ""}))

                loop = asyncio.get_running_loop()
                mic_q: asyncio.Queue = asyncio.Queue()
                stream = make_mic_stream(loop, mic_q)
                stream.start()

                async def sender():
                    while True:
                        data = await mic_q.get()
                        await ws.send(data)

                ptask = asyncio.create_task(player.run())
                tasks = [
                    asyncio.create_task(sender()),
                    asyncio.create_task(recv_loop(ws, player)),
                    asyncio.create_task(keyboard_loop(ws, stop_ev)),
                ]
                await stop_ev.wait()
                for t in tasks:
                    t.cancel()
                ptask.cancel()
                stream.stop()
                stream.close()
                break
        except ConnectionClosed:
            print(f"🔌 连接断开，重连中…（第 {retry + 1} 次）", flush=True)
        except OSError as e:
            print(f"⚠️ 无法连接 {args.host}:{args.port} — {e}", flush=True)
            print("   请确认服务器已启动（python app.py）且防火墙放行 TCP 端口", flush=True)
        retry += 1
        await asyncio.sleep(2.0)

    import pygame

    pygame.mixer.quit()
    print("\n再见！", flush=True)


def main():
    parser = argparse.ArgumentParser(description="小音 CLI 客户端（连接语音助手服务器）")
    parser.add_argument("--host", default="127.0.0.1", help="服务器地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=7860, help="服务器端口（默认 7860）")
    parser.add_argument("--voice", default="", help="TTS 音色，如 zh-CN-XiaoyiNeural（可选）")
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n再见！", flush=True)


if __name__ == "__main__":
    main()
