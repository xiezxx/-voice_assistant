# -*- coding: utf-8 -*-
"""语音会话客户端共享音频组件（CLI client.py 与桌面宠物 pet.py 共用）。

- PygamePlayer：mp3 字节 → 临时文件 → 队列顺序播放，stop() 打断清空。
- make_mic_stream：sounddevice 回调线程 → 指定事件循环队列（int16 bytes）。
注意：pre_init(44100,-16,2,512) 必须在 mixer.init() 前（Windows MP3 播放）。
"""

import asyncio
import os
import tempfile

import numpy as np
import sounddevice as sd

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
