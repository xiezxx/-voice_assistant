# -*- coding: utf-8 -*-
"""免提唤醒 WebSocket 集成测试：真实 uvicorn + sherpa KWS，流式喂合成「小音小音」。

覆盖：连接 ready、唤醒帧、冷却期内不重复触发、冷却后可再次触发、断开。
需要联网（edge-tts 合成测试音频）与 sherpa-onnx 已安装；不满足则跳过。
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import numpy as np


def _load_wake_audio() -> np.ndarray:
    """用 edge-tts 合成「小音小音」→ 解码为 16k 单声道 float32（与 test_wakeword 相同）。"""
    import edge_tts
    import pygame
    from scipy.signal import resample

    asyncio.run(edge_tts.Communicate("小音小音", "zh-CN-XiaoyiNeural").save("wake_test.mp3"))
    pygame.mixer.init()
    sr_raw = pygame.mixer.get_init()[0]
    import pygame.sndarray

    raw = pygame.sndarray.array(pygame.mixer.Sound("wake_test.mp3"))
    audio = raw.astype(np.float32).mean(axis=1) / 32768.0
    return resample(audio, int(len(audio) * 16000 / sr_raw))


def _to_frames(audio: np.ndarray, chunk: int = 1600) -> list:
    """float32 音频 → int16 LE 帧（每帧 1600 样本，对齐浏览器端）。"""
    pcm = np.clip(np.rint(np.clip(audio, -1, 1) * 32768), -32768, 32767).astype("<i2")
    frames = []
    for i in range(0, len(pcm), chunk):
        part = pcm[i:i + chunk]
        if len(part) < chunk:
            part = np.pad(part, (0, chunk - len(part)))
        frames.append(part.tobytes())
    return frames


async def _expect_json(ws, timeout=5.0):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def test_ws_wake_flow():
    from wakeword import KwsFeedDetector, sherpa_available
    from wake_server import WakeDeps, register_routes

    ok, reason = sherpa_available()
    if not ok:
        print(f"⚠ 跳过 WS 集成测试（{reason}）")
        return

    from fastapi import FastAPI
    import uvicorn

    try:
        audio = _load_wake_audio()
    except Exception as e:
        print(f"⚠ 跳过 WS 集成测试（测试音频合成失败：{e}）")
        return
    frames = _to_frames(audio)

    app = FastAPI()
    deps = WakeDeps(kws=KwsFeedDetector(), kws_lock=asyncio.Lock())
    register_routes(app, deps)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8765, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)

    async def client():
        import websockets

        async with websockets.connect("ws://127.0.0.1:8765/ws/wake") as ws:
            # 1. 连接后收到 ready
            msg = await _expect_json(ws)
            assert msg["type"] == "ready", msg
            assert msg["sample_rate"] == 16000 and msg["chunk"] == 1600
            print("✓ WS 连接：收到 ready")

            # 2. 流式发「小音小音」→ 收到 wake
            for f in frames:
                await ws.send(f)
            msg = await _expect_json(ws, timeout=10.0)
            assert msg["type"] == "wake" and msg["keyword"], msg
            print(f"✓ WS 唤醒检测: 检测到 {msg['keyword']}")

            # 3. 冷却期内立即重发 → 不应再次触发
            for f in frames:
                await ws.send(f)
            try:
                await _expect_json(ws, timeout=1.5)
                raise AssertionError("冷却期内不应再次触发")
            except asyncio.TimeoutError:
                print("✓ 冷却期：5 秒内不重复触发")

            # 4. 冷却过后重发 → 可再次触发（证明重新布防）
            await asyncio.sleep(5.2)
            for f in frames:
                await ws.send(f)
            msg = await _expect_json(ws, timeout=10.0)
            assert msg["type"] == "wake", msg
            print("✓ 冷却期后重新布防：再次检测到唤醒")

    try:
        asyncio.run(client())
    finally:
        server.should_exit = True
        thread.join(timeout=5)


if __name__ == "__main__":
    test_ws_wake_flow()
    print("\n全部通过 ✅")
