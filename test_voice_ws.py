# -*- coding: utf-8 -*-
"""语音会话 WebSocket 集成测试：Fake 依赖注入覆盖全协议 + 门控的真实全链路。

Fake 测试覆盖：ready/hello、唤醒+冷却、完整轮次、说话打断、文字控制、停止控制。
真实集成（需 DeepSeek Key + sherpa 可用 + 联网）：合成语音全流程。
"""

import asyncio
import json
import threading
import time
from pathlib import Path

import numpy as np

# 防止 Fake 测试污染真实会话文件（voice_server 在导入时绑定了函数名）
import voice_server
import conversation_store

voice_server.save_conversation = lambda history, conversation: None
voice_server.clear_conversation = lambda: None


# ── Fake 依赖 ───────────────────────────────────────────────

class FakeKws:
    """第 N 次 feed 触发唤醒（fire_on 可配置），其余不触发。"""

    def __init__(self, fire_on=(1,)):
        self.sample_rate = 16000
        self.calls = 0
        self.fire_on = set(fire_on)

    def create_stream(self):
        return object()

    def feed(self, stream, samples):
        self.calls += 1
        return "小音" if self.calls in self.fire_on else ""


class AlwaysKws(FakeKws):
    """每次都触发唤醒（用于验证服务器端冷却与重新布防）。"""

    def feed(self, stream, samples):
        self.calls += 1
        return "小音"


class FakeSTT:
    def transcribe(self, audio, sample_rate=None):
        return "现在几点"


class FastBot:
    status = ""
    conversation = []

    def __init__(self):
        self.conversation = []

    def reset(self):
        self.conversation = []

    async def chat_stream(self, user_text):
        yield "现在是下午"
        await asyncio.sleep(0.05)
        yield "三点。"


class SlowBot(FastBot):
    """第一句后长暂停，供打断测试在回复中途介入。"""

    async def chat_stream(self, user_text):
        yield "第一句。"
        await asyncio.sleep(30)
        yield "第二句。"


class FakeTTS:
    VOICE = ""

    async def synthesize_stream(self, text):
        return b"\xff\xfb" + text.encode("utf-8")


class EmptySTT(FakeSTT):
    """识别返回空（安静/听不清时的真实路径）。"""

    def transcribe(self, audio, sample_rate=None):
        return ""


class FakeSpeaker:
    """声纹锁定 Fake：嵌入 = 音频平均电平；匹配 = 电平差 < threshold（0.2）。

    用电平 0.5 的帧当「主人」，0.05 的帧当「别人」。
    """

    threshold = 0.2

    class _Manager:
        def __init__(self):
            self.emb = None

        def add(self, name, emb):
            self.emb = emb

        def search(self, emb, threshold):
            if self.emb is None:
                return ""
            diff = abs(float(emb[0]) - float(self.emb[0]))
            return "主人" if diff < threshold else ""

    def create_manager(self):
        return self._Manager()

    def extract(self, samples):
        return [float(np.abs(samples).mean())]

    def verify(self, manager, samples):
        return bool(manager.search(self.extract(samples), self.threshold))


def _make_deps(bot=None, kws=None, stt=None, speaker=None):
    return voice_server.VoiceDeps(
        kws=kws if kws is not None else FakeKws(),
        stt=stt if stt is not None else FakeSTT(),
        bot=bot if bot is not None else FastBot(),
        tts=FakeTTS(),
        speaker=speaker,
        history=[],
        get_voice=lambda: "zh-CN-XiaoyiNeural",
        turn_lock=asyncio.Lock(),
        stt_lock=asyncio.Lock(),
        kws_lock=asyncio.Lock(),
        speaker_lock=asyncio.Lock(),
        queue=asyncio.Queue(),
    )


def _run_server(deps) -> tuple:
    from fastapi import FastAPI
    import uvicorn

    app = FastAPI()
    voice_server.register_routes(app, deps)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8766, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    return server, thread


def _stop_server(server, thread):
    server.should_exit = True
    thread.join(timeout=5)


async def _connect():
    import websockets

    ws = await websockets.connect("ws://127.0.0.1:8766/ws/assistant")
    return ws


async def _expect(ws, timeout=8.0):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    if isinstance(raw, bytes):
        return raw
    return json.loads(raw)


def _frame(level: float, n: int = 1600) -> bytes:
    pcm = (np.clip(np.full(n, level, dtype=np.float32), -1, 1) * 32768).astype("<i2")
    return pcm.tobytes()


async def _send_speech(ws, loud=6, silence=None):
    """唤醒后的典型话语：0.6s 语音 + 尾静音（按 .env 配置的 SILENCE_DURATION 自适应）。"""
    from config import Config

    if silence is None:
        silence = int(Config.SILENCE_DURATION * 10) + 2
    for _ in range(loud):
        await ws.send(_frame(0.5))
    for _ in range(silence):
        await ws.send(_frame(0.0))


async def _expect_transcript(ws, timeout=15.0):
    """等待 transcript 事件（跳过 mp3 二进制帧与 status 等中间事件）。"""
    while True:
        msg = await _expect(ws, timeout)
        if isinstance(msg, bytes):
            continue
        if msg["type"] == "transcript":
            return msg


async def _expect_silence(ws, timeout=1.2):
    """期望超时内无任何消息。"""
    try:
        await asyncio.wait_for(ws.recv(), timeout)
        raise AssertionError("不应收到消息")
    except asyncio.TimeoutError:
        pass


# ── 测试 ────────────────────────────────────────────────────

async def _test_hello_wake():
    deps = _make_deps()
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            msg = await _expect(ws)
            assert msg["type"] == "ready" and msg["kws"] is True, msg
            await ws.send(json.dumps({"type": "hello", "voice": "zh-CN-XiaoxiaoNeural"}))
            msg = await _expect(ws)
            assert msg == {"type": "hello_ok", "voice": "zh-CN-XiaoxiaoNeural"}, msg
            # 第一帧触发唤醒
            await ws.send(_frame(0.1))
            msg = await _expect(ws)
            assert msg["type"] == "wake" and msg["keyword"], msg
    finally:
        _stop_server(server, thread)
    print("✓ ready/hello 配置音色 + 唤醒事件")


async def _test_cooldown_rearm():
    deps = _make_deps(kws=AlwaysKws())
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(_frame(0.1))
            assert (await _expect(ws))["type"] == "wake"
            await _send_speech(ws)
            await _expect_transcript(ws)
            while True:
                msg = await _expect(ws, timeout=10.0)
                if isinstance(msg, bytes):
                    continue  # mp3 二进制帧
                if msg["type"] == "turn_end":
                    break
            # 轮次结束回 IDLE：冷却期内喂帧（KWS 每次都命中）不应唤醒
            for _ in range(10):
                await ws.send(_frame(0.1))
            await _expect_silence(ws, timeout=1.5)
            # 冷却过后重新布防：再喂帧应唤醒
            await asyncio.sleep(5.2)
            await ws.send(_frame(0.1))
            assert (await _expect(ws, timeout=10.0))["type"] == "wake"
    finally:
        _stop_server(server, thread)
    print("✓ 唤醒冷却：轮次结束后 5s 内不重复触发，冷却过后重新布防")


async def _test_full_turn():
    deps = _make_deps()
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(_frame(0.1))          # 唤醒
            assert (await _expect(ws))["type"] == "wake"
            await _send_speech(ws)
            msg = await _expect_transcript(ws)
            assert msg["text"] == "现在几点", msg
            events = []
            while True:
                msg = await _expect(ws, timeout=10.0)
                if isinstance(msg, bytes):
                    events.append(("audio_bytes", msg))
                    continue
                events.append((msg["type"], msg))
                if msg["type"] == "turn_end":
                    break
            types = [t for t, _ in events]
            assert types.count("sentence") >= 1 and types.count("audio_bytes") >= 1, types
            mp3 = next(m for t, m in events if t == "audio_bytes")
            assert mp3.startswith(b"\xff\xfb"), "mp3 魔数不符"
            turn_end = next(m for t, m in events if t == "turn_end")
            assert turn_end["stopped"] is False, turn_end
            assert deps.history[-1]["content"].startswith("🤖 现在"), deps.history[-1]
            assert deps.history[0] == {"role": "user", "content": "🎤 现在几点"}, deps.history[0]
            # 队列收到轮次快照（sentences 为空：音频由客户端播放）
            item = deps.queue.get_nowait()
            assert item["sentences"] == [] and item["history"][-1] == deps.history[-1]
    finally:
        _stop_server(server, thread)
    print("✓ 完整轮次：唤醒→话语→transcript→逐句 sentence+mp3→turn_end→队列快照")


async def _test_barge_in():
    deps = _make_deps(bot=SlowBot())
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(_frame(0.1))
            assert (await _expect(ws))["type"] == "wake"
            await _send_speech(ws)
            await _expect_transcript(ws)
            # 第一句出现后，回复卡在长暂停处；发 0.4s 语音触发打断
            msg = await _expect(ws, timeout=10.0)
            assert msg["type"] == "sentence", msg
            for _ in range(4):
                await ws.send(_frame(0.5))
            got_barge = False
            while True:
                msg = await _expect(ws, timeout=10.0)
                if isinstance(msg, bytes):
                    continue  # mp3 二进制帧
                if msg["type"] == "barge_in":
                    got_barge = True
                elif msg["type"] == "turn_end":
                    assert msg["stopped"] is True, msg
                    break
            assert got_barge, "应收到 barge_in"
            # 无需再唤醒：继续说 → 第二次 transcript
            await _send_speech(ws)
            await _expect_transcript(ws)
            # 部分回复已补录进服务端历史（打断后上下文完整）
            assert any(
                m.get("role") == "assistant" and "第一句" in str(m.get("content"))
                for m in deps.history
            ), deps.history
    finally:
        _stop_server(server, thread)
    print("✓ 打断：回复中说话→barge_in→无需再唤醒直接新一轮；部分回复已补录")


async def _test_text_control():
    deps = _make_deps()
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(json.dumps({"type": "text", "text": "你好"}))
            msg = await _expect(ws)
            assert msg["type"] == "transcript" and msg["text"] == "你好", msg
            while True:
                msg = await _expect(ws, timeout=10.0)
                if isinstance(msg, bytes):
                    continue  # mp3 二进制帧
                if msg["type"] == "turn_end":
                    break
            assert deps.history[0] == {"role": "user", "content": "⌨️ 你好"}, deps.history[0]
    finally:
        _stop_server(server, thread)
    print("✓ 文字控制：text → 直接进入回复轮次（⌨️ 前缀）")


async def _test_stop_control():
    # 第 1 帧唤醒；stop 后回 IDLE，第 40 次 KWS feed 再次唤醒（验证重新布防）
    deps = _make_deps(bot=SlowBot(), kws=FakeKws(fire_on=(1, 40)))
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(_frame(0.1))
            assert (await _expect(ws))["type"] == "wake"
            await _send_speech(ws)
            await _expect_transcript(ws)
            assert (await _expect(ws, timeout=10.0))["type"] == "sentence"
            await ws.send(json.dumps({"type": "stop"}))
            while True:
                msg = await _expect(ws, timeout=10.0)
                if isinstance(msg, bytes):
                    continue  # mp3 二进制帧
                if msg["type"] == "turn_end":
                    assert msg["stopped"] is True, msg
                    break
            # stop 后回 IDLE；等唤醒冷却过期后继续喂帧（第 40 次 KWS feed）应再次唤醒
            await asyncio.sleep(5.2)
            for _ in range(50):
                await ws.send(_frame(0.5))
                await asyncio.sleep(0.02)  # 给服务器状态机切换留时间
            msg = await _expect(ws, timeout=10.0)
            assert msg["type"] == "wake", msg
    finally:
        _stop_server(server, thread)
    print("✓ 停止控制：stop→turn_end(stopped)→回 IDLE 重新布防唤醒")


async def _test_speaker_lock():
    """声纹锁定：首唤醒录入 → 别人唤醒被拒 → 主人唤醒通过。"""
    deps = _make_deps(kws=FakeKws(fire_on=(1, 2, 3)), speaker=FakeSpeaker())
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            # 1. 主人（电平 0.5）首次唤醒：录入 + 正常唤醒
            await ws.send(_frame(0.5))
            assert (await _expect(ws))["type"] == "wake"
            await _send_speech(ws)
            await _expect_transcript(ws)
            while True:
                msg = await _expect(ws, timeout=10.0)
                if isinstance(msg, bytes):
                    continue
                if msg["type"] == "turn_end":
                    break
            # 2. 冷却过后，别人（电平 0.05）唤醒：拒绝且不触发 wake
            await asyncio.sleep(5.2)
            await ws.send(_frame(0.05))
            msg = await _expect(ws, timeout=10.0)
            assert msg["type"] == "speaker_reject", msg
            await _expect_silence(ws, timeout=1.2)
            # 3. 等拒绝冷却过期后，主人再唤醒：通过
            await asyncio.sleep(5.2)
            await ws.send(_frame(0.5))
            msg = await _expect(ws, timeout=10.0)
            assert msg["type"] == "wake", msg
    finally:
        _stop_server(server, thread)
    print("✓ 声纹锁定：首唤醒录入/他人拒绝/主人通过")


async def _test_listen_control():
    """点击唤醒：listen 免唤醒词进聆听（source=click）；声纹锁不拦显式点击；轮次中忽略。"""
    deps = _make_deps(speaker=FakeSpeaker())
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(json.dumps({"type": "listen"}))
            msg = await _expect(ws)
            assert msg["type"] == "wake" and msg.get("source") == "click", msg
            await ws.send(json.dumps({"type": "listen"}))  # LISTENING 中再点 → 忽略
            await _send_speech(ws)
            await _expect_transcript(ws)
            while True:
                msg = await _expect(ws, timeout=20.0)
                if isinstance(msg, bytes):
                    continue
                if msg["type"] == "turn_end":
                    break
    finally:
        _stop_server(server, thread)
    print("✓ 点击唤醒：listen 免唤醒词进聆听（source=click；轮次中忽略）")


async def _test_empty_stt_feedback():
    """识别为空时：必须给用户明确反馈（不能静默回待机）。"""
    deps = _make_deps(stt=EmptySTT())
    server, thread = _run_server(deps)
    try:
        async with await _connect() as ws:
            await _expect(ws)  # ready
            await ws.send(_frame(0.1))
            assert (await _expect(ws))["type"] == "wake"
            await _send_speech(ws)
            # 先收到「正在识别…」，随后收到「没识别到内容」提示
            msg = None
            while True:
                msg = await _expect_transcript_ish(ws)
                if msg["type"] == "transcript" or "没识别" in msg.get("text", ""):
                    break
            assert msg["type"] == "status" and "没识别" in msg["text"], msg
            # 回待机：冷却期内不再唤醒（AlwaysKws 场景由冷却测试覆盖，此处确认无事件）
            await _expect_silence(ws, timeout=1.2)
    finally:
        _stop_server(server, thread)
    print("✓ 空识别反馈：status 提示后回待机（不静默）")


async def _expect_transcript_ish(ws, timeout=15.0):
    """等待 transcript 或 status 事件（跳过 mp3 二进制帧）。"""
    while True:
        msg = await _expect(ws, timeout)
        if isinstance(msg, bytes):
            continue
        if msg["type"] in ("transcript", "status"):
            return msg


def _test_real_integration():
    """真实全链路：真实 KWS/Whisper/DeepSeek/Edge-TTS + 合成语音（需联网与 Key）。"""
    from config import Config
    from wakeword import KwsFeedDetector, sherpa_available

    ok, reason = sherpa_available()
    if not ok:
        print(f"⚠ 跳过真实集成（{reason}）")
        return
    if not Config.DEEPSEEK_API_KEY:
        print("⚠ 跳过真实集成（未配置 DEEPSEEK_API_KEY）")
        return

    from stt import SpeechRecognizer
    from llm import ChatBot
    from tts import SpeechSynthesizer

    # 备份真实会话文件，测试后恢复
    store_file = Path(__file__).parent / "data" / "conversation.json"
    backup = store_file.read_bytes() if store_file.exists() else None

    stt = SpeechRecognizer()
    stt.load()
    deps = voice_server.VoiceDeps(
        kws=KwsFeedDetector(), stt=stt, bot=ChatBot(), tts=SpeechSynthesizer(),
        history=[], get_voice=lambda: "zh-CN-XiaoyiNeural",
        turn_lock=asyncio.Lock(), stt_lock=asyncio.Lock(), kws_lock=asyncio.Lock(),
        queue=asyncio.Queue(),
    )
    # 恢复真实保存行为（本测试只此一次）
    voice_server.save_conversation = conversation_store.save_conversation

    def _load_audio(text: str) -> np.ndarray:
        import edge_tts
        import pygame
        from scipy.signal import resample

        asyncio.run(edge_tts.Communicate(text, "zh-CN-XiaoyiNeural").save("wake_test.mp3"))
        pygame.mixer.init()
        sr_raw = pygame.mixer.get_init()[0]
        import pygame.sndarray

        raw = pygame.sndarray.array(pygame.mixer.Sound("wake_test.mp3"))
        audio = raw.astype(np.float32).mean(axis=1) / 32768.0
        return resample(audio, int(len(audio) * 16000 / sr_raw))

    def _frames_of(audio: np.ndarray) -> list:
        pcm = np.clip(np.rint(np.clip(audio, -1, 1) * 32768), -32768, 32767).astype("<i2")
        return [
            pcm[i:i + 1600].tobytes()
            for i in range(0, len(pcm) - 1599, 1600)
        ]

    try:
        wake_audio = _load_audio("小音小音")
        q_audio = _load_audio("现在几点")
    except Exception as e:
        print(f"⚠ 跳过真实集成（测试音频合成失败：{e}）")
        return

    server, thread = _run_server(deps)

    async def client():
        import websockets

        async with websockets.connect("ws://127.0.0.1:8766/ws/assistant") as ws:
            await _expect(ws)  # ready
            for f in _frames_of(wake_audio):
                await ws.send(f)
            msg = await _expect(ws, timeout=15.0)
            assert msg["type"] == "wake", msg
            for f in _frames_of(q_audio):
                await ws.send(f)
            for _ in range(int(Config.SILENCE_DURATION * 10) + 3):  # 尾静音补足
                await ws.send(_frame(0.0))
            msg = await _expect_transcript(ws, timeout=30.0)
            # Whisper 可能输出繁体（幾點/現在），按语义断言
            assert "点" in msg["text"] or "點" in msg["text"], msg
            got_sentence = got_mp3 = False
            while True:
                msg = await _expect(ws, timeout=60.0)
                if isinstance(msg, bytes):
                    got_mp3 = True
                    continue
                if msg["type"] == "sentence":
                    got_sentence = True
                if msg["type"] == "turn_end":
                    break
            assert got_sentence and got_mp3, "应收到逐句文本与 mp3"
    try:
        asyncio.run(client())
        print("✓ 真实全链路：合成「小音小音」→ 唤醒 → 「现在几点」→ 识别 → 回复 → mp3")
    finally:
        _stop_server(server, thread)
        if backup is not None:
            store_file.write_bytes(backup)
        elif store_file.exists():
            store_file.unlink()


def main():
    asyncio.run(_test_hello_wake())
    asyncio.run(_test_cooldown_rearm())
    asyncio.run(_test_full_turn())
    asyncio.run(_test_barge_in())
    asyncio.run(_test_text_control())
    asyncio.run(_test_stop_control())
    asyncio.run(_test_empty_stt_feedback())
    asyncio.run(_test_speaker_lock())
    asyncio.run(_test_listen_control())
    _test_real_integration()
    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
