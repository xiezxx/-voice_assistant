# -*- coding: utf-8 -*-
"""免提唤醒 Web 服务端：WS 音频流 KWS 检测 + /api/wake_audio 全流水线 + 播放队列。

无 gradio 导入，可独立测试。由 app.py 通过 register_routes(demo.app, deps) 挂载。

协议：
- WS /ws/wake：客户端发二进制帧（int16 LE 单声道 16k，每帧 1600 样本 = 0.1 秒）；
  服务器回 JSON 文本帧 {"type":"ready",...} / {"type":"wake","keyword":"小音"}。
- POST /api/wake_audio：请求体为 WAV 字节（16-bit PCM 单声道），完整跑
  STT→LLM→逐句 TTS 管线后把整轮结果压入队列，返回 {"ok":true,"user_text":...}。
"""

import asyncio
import io
import os
import time
import wave
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from fastapi import Request, WebSocket
from fastapi.responses import JSONResponse

from conversation_store import save_conversation
from speech_utils import sentence_stream

WS_PATH = "/ws/wake"
AUDIO_PATH = "/api/wake_audio"
MAX_BODY = 2_000_000          # 12s 16k 单声道 P16 WAV ≈ 384KB，防御性上限
FRAME_BYTES = 1600 * 2        # 0.1s int16 单声道
MAX_BUFFER_BYTES = 100_000    # 异常客户端缓冲上限，超限清空
KWS_COOLDOWN_SEC = 5.0        # 单连接唤醒冷却，防止同一次唤醒反复触发


@dataclass
class WakeDeps:
    """唤醒服务的依赖集合；由 app.py 组装后传入 register_routes。"""

    kws: "KwsFeedDetector | None" = None      # WS 必需；None 时连接报错
    stt: object = None
    bot: object = None
    tts: object = None
    history: Optional[list] = None            # SERVER_HISTORY（与 app.py 共享）
    get_voice: Optional[Callable[[], str]] = None
    turn_lock: Optional[asyncio.Lock] = None  # 串行化所有轮次（手动/免提共用）
    stt_lock: Optional[asyncio.Lock] = None   # faster-whisper 串行化
    kws_lock: Optional[asyncio.Lock] = None   # sherpa decode 串行化（跨 WS 连接）
    queue: Optional[asyncio.Queue] = None     # 完成的轮次入队，gr.Timer 消费
    _locks_ready: bool = field(default=False, repr=False)


def wav_bytes_to_audio(data: bytes) -> Optional[tuple]:
    """16-bit PCM 单声道 WAV → (float32 一维数组, 采样率)；格式不符/异常返回 None。"""
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            sr = w.getframerate()
            ok = (
                w.getsampwidth() == 2
                and w.getnchannels() == 1
                and 8000 <= sr <= 48000
                and w.getnframes() > 0
            )
            if not ok:
                return None
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError, OSError, ValueError):
        return None
    audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return audio, sr


def unlink_path(path: str):
    """尽力删除临时音频文件，忽略任何失败。"""
    try:
        os.unlink(path)
    except OSError:
        pass


def pop_latest_turn(queue) -> Optional[dict]:
    """清空队列、保留最新一轮；被丢弃轮次的 mp3 立即删除。"""
    latest = None
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        if latest is not None:
            for s in latest["sentences"]:
                unlink_path(s["path"])
        latest = item
    return latest


def should_advance(ready_at: float, now: float, paused: bool) -> bool:
    """纯函数：上一句播放窗口已过且未暂停 → 可播下一句。便于离线测试。"""
    return (not paused) and now >= ready_at


def register_routes(app, deps: WakeDeps):
    """在 FastAPI app 上注册 WS 唤醒检测与 WAV 上传路由（须在 launch 前调用）。"""

    async def _ws(ws: WebSocket):
        await ws.accept()
        if deps.kws is None:
            await ws.send_json({"type": "error", "error": "KWS 模型不可用"})
            await ws.close()
            return
        stream = deps.kws.create_stream()
        await ws.send_json({"type": "ready", "sample_rate": 16000, "chunk": 1600})
        buf = b""
        cooldown_until = 0.0
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") != "websocket.receive":
                    break  # websocket.disconnect / 其他
                if msg.get("bytes") is None:
                    continue  # 忽略文本帧
                buf += msg["bytes"]
                if len(buf) > MAX_BUFFER_BYTES:
                    buf = b""  # 异常客户端保护
                    continue
                while len(buf) >= FRAME_BYTES:
                    frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                    samples = (
                        np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
                    )
                    async with deps.kws_lock:
                        keyword = deps.kws.feed(stream, samples)
                    if keyword and time.time() >= cooldown_until:
                        cooldown_until = time.time() + KWS_COOLDOWN_SEC
                        print(f"[免提唤醒] 检测到: {keyword}", flush=True)
                        await ws.send_json({"type": "wake", "keyword": keyword})
        except Exception:
            pass
        finally:
            try:
                await ws.close()
            except Exception:
                pass

    app.add_api_websocket_route(WS_PATH, _ws)

    if deps.stt is None:
        return  # 无流水线依赖时只提供 KWS 检测

    async def _post(request: Request):
        try:
            data = await request.body()
        except Exception:
            return JSONResponse({"ok": False, "error": "读取请求体失败"}, status_code=400)
        if len(data) > MAX_BODY:
            return JSONResponse({"ok": False, "error": "音频过大"}, status_code=400)
        decoded = wav_bytes_to_audio(data)
        if decoded is None:
            return JSONResponse(
                {"ok": False, "error": "需要 16-bit PCM 单声道 WAV"}, status_code=400
            )
        audio, sr = decoded

        async with deps.turn_lock:
            async with deps.stt_lock:
                user_text = await asyncio.to_thread(deps.stt.transcribe, audio, sr)
            if not user_text:
                return {"ok": True, "empty": True}

            deps.history.append({"role": "user", "content": f"🎤 {user_text}"})
            deps.history.append({"role": "assistant", "content": ""})
            voice = deps.get_voice() if deps.get_voice else deps.tts.VOICE
            deps.tts.VOICE = voice

            parts: list[str] = []
            sentences: list[dict] = []  # [{text, path}] 供 Timer 逐句播报
            status = "✅ 已完成回复"
            try:
                async for sentence in sentence_stream(deps.bot.chat_stream(user_text)):
                    parts.append(sentence)
                    deps.history[-1]["content"] = "🤖 " + "".join(parts)
                    try:
                        path = await deps.tts.synthesize(sentence)
                        sentences.append({"text": sentence, "path": path})
                    except Exception:
                        continue  # 合成失败：跳过播报，文字已显示
            except Exception as e:
                deps.history[-1]["content"] = f"⚠️ LLM 调用失败：{e}"
                status = "⚠️ LLM 调用失败"
                sentences = []
            if not parts:
                deps.history[-1]["content"] = "🤖 （暂无回复）"
                status = "⚠️ 模型返回空回复"

            save_conversation(deps.history, deps.bot.conversation)
            deps.queue.put_nowait(
                {
                    "sentences": sentences,
                    "status": status,
                    "history": list(deps.history),  # 快照，供 Timer 写回 chatbot
                }
            )
        return {"ok": True, "user_text": user_text}

    app.add_api_route(AUDIO_PATH, _post, methods=["POST"])
