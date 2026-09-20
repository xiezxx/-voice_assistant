# -*- coding: utf-8 -*-
"""全双工语音会话 WebSocket 服务端（/ws/assistant）。

协议（详见 README「接入协议」）：
- 客户端 → 服务器：二进制帧 = int16 LE 单声道 16k PCM（任意帧大小，服务器缓冲切分，
  0.1s/1600 样本一块）；JSON 控制：hello / text / stop / reset / listen（点击免唤醒词聆听，
  仅待机生效）。麦克风持续推流（含 AI 播报期间，供服务器检测说话打断）。
- 服务器 → 客户端：JSON 事件 ready / hello_ok / wake / transcript / status / sentence /
  audio / barge_in / turn_end / error；audio 事件后紧跟一个二进制帧 = 该句完整 mp3。

无 gradio 导入，可独立测试。由 app.py 通过 register_routes(demo.app, deps) 挂载。

会话为 actor 模型：rx_task 收帧/控制并路由（KWS/采集/打断检测只在此任务内），
主循环消费事件推进状态机并发送事件。历史上下文为服务器全局共享（VoiceDeps.history）。
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
from fastapi import WebSocket

from config import Config
from conversation_store import save_conversation, clear_conversation
from speech_utils import sentence_stream

logger = logging.getLogger("voice")

WS_PATH = "/ws/assistant"
SAMPLE_RATE = 16000
CHUNK = 1600                      # 每块样本数（0.1s）
FRAME_BYTES = CHUNK * 2           # int16 单声道
MAX_BUFFER_BYTES = 100_000        # 异常客户端缓冲上限
KWS_COOLDOWN_SEC = 5.0            # 单连接唤醒冷却
# 声纹拒绝后的提示抑制窗口（只是"别重复刷提示"，不再拦校验 —— 见 feed() 里的注释）
REJECT_COOLDOWN_SEC = 2.0
ADAPT_THRESHOLD = 0.75            # 声纹相似度高于此值时，把本次并进声纹中心（自适应更新）
VAD_THRESHOLD = Config.VAD_THRESHOLD        # 能量门阈值（对齐 audio_utils.py）
UTTERANCE_SILENCE_SEC = Config.SILENCE_DURATION  # 尾静音判定（.env 可调）
UTTERANCE_MIN_SEC = 0.3           # 最短有效话语
UTTERANCE_MAX_SEC = 12.0          # 最长录制
BARGE_STREAK = 3                  # 连续 0.3s 有语音判定打断
# 打断门限 = 聆听门限 × 这个倍数。
# 必须比聆听门高：聆听门要低到"词与词的停顿"也算还在说话，而打断是用户**主动提高音量**
# 插话，本来就比正常说话响。两者共用一个门限时怎么调都是拆东墙补西墙（手机热点那台
# 用户反馈"说话被切断"和"播报被别的声音打断"同时出现，就是这个原因）。
BARGE_MULT = 2.0
# 每发一句音频后的"打断静默窗"（秒）：刚起播那一下自己的外放最响，最容易把自己的回声
# 当成用户插话。手机上的 AEC 只对通话路径有效，媒体外放拿不到参考信号，所以只能靠这个。
BARGE_GRACE_SEC = 0.4
SEND_TIMEOUT = 10.0               # 单条消息发送超时（背压保护，超时中止轮次）
LISTEN_NO_SPEECH_TIMEOUT = 15.0   # 唤醒后一直没听到声音：超时提示并回待机
CONTINUOUS_TIMEOUT = Config.CONTINUOUS_TIMEOUT  # 连续对话：回复完后等下一句的秒数（.env 可调）


class _SendAborted(Exception):
    """发送超时/失败：中止当前轮次。"""


@dataclass
class VoiceDeps:
    """会话服务依赖集合；由 app.py 组装后传入 register_routes。"""

    kws: "KwsFeedDetector | None" = None
    stt: object = None
    bot: object = None
    tts: object = None
    speaker: object = None                    # 声纹锁定（SpeakerVerifier 或 None）
    history: Optional[list] = None            # SERVER_HISTORY（全局共享）
    get_voice: Optional[Callable[[], str]] = None
    turn_lock: Optional[asyncio.Lock] = None  # 串行化所有轮次
    stt_lock: Optional[asyncio.Lock] = None   # faster-whisper 串行化
    kws_lock: Optional[asyncio.Lock] = None   # sherpa decode 串行化（跨连接）
    speaker_lock: Optional[asyncio.Lock] = None  # 声纹提取串行化（跨连接）
    queue: Optional[asyncio.Queue] = None     # 轮次快照入队，gr.Timer 消费
    _locks_ready: bool = field(default=False, repr=False)


# ── 通用 helper（供 app.py 复用）────────────────────────────

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
            for s in latest.get("sentences") or []:
                unlink_path(s["path"])
        latest = item
    return latest


def should_advance(ready_at: float, now: float, paused: bool) -> bool:
    """纯函数：上一句播放窗口已过且未暂停 → 可播下一句。便于离线测试。"""
    return (not paused) and now >= ready_at


def should_follow_up(continuous: bool, result: str) -> bool:
    """纯函数：这一轮结束后要不要免唤醒继续听下一句。

    - 「被用户打断」（barged）后继续听是**既有行为**，与连续模式开关无关，别动它；
    - 「正常回复完」（done）只有开了连续模式才继续；
    - timeout（进聆听后一直没人说话）/ too_short / empty 一律退出——这些情况下用户
      并没有要接着说，放行会让环境噪声无限刷轮次（忙循环刷屏）。
    """
    if result == "barged":
        return True
    return continuous and result == "done"


# ── 纯函数组件（可离线单测）─────────────────────────────────

def iter_frames(buf: bytes) -> tuple:
    """把字节缓冲切成 FRAME_BYTES 对齐的帧，返回 (帧列表, 余留字节)。"""
    if len(buf) < FRAME_BYTES:
        return [], buf
    n = len(buf) // FRAME_BYTES
    return [buf[i * FRAME_BYTES:(i + 1) * FRAME_BYTES] for i in range(n)], buf[n * FRAME_BYTES:]


class UtteranceCollector:
    """能量门 VAD 采集器：喂 0.1s 块，尾静音判定完成（参数对齐 audio_utils）。

    threshold 可按唤醒时的语音电平校准传入（说话轻的人用更低的阈值）。
    """

    def __init__(self, threshold: float = None):
        self._threshold = VAD_THRESHOLD if threshold is None else threshold
        self._frames: list[np.ndarray] = []
        self._has_speech = False
        self._silence = 0
        self._max_frames = int(UTTERANCE_MAX_SEC * 10)
        self._silence_needed = int(UTTERANCE_SILENCE_SEC * 10)
        self.done = False

    def feed(self, samples: np.ndarray) -> bool:
        """喂入一块（建议 1600 样本），返回本块后话语是否采集完成。"""
        if self.done:
            return True
        level = float(np.abs(samples).mean())
        if not self._has_speech:
            if level >= self._threshold:
                self._has_speech = True
                self._frames.append(samples)
            else:
                # 前置静音：只保留一段预卷，丢弃最老的
                self._frames.append(samples)
                if len(self._frames) > self._silence_needed:
                    self._frames.pop(0)
            return False
        self._frames.append(samples)
        if level < self._threshold:
            self._silence += 1
            if self._silence >= self._silence_needed:
                self.done = True
        else:
            self._silence = 0
        if len(self._frames) >= self._max_frames:
            self.done = True
        return self.done

    def take(self) -> np.ndarray:
        """取出已采集的音频（float32 一维）。"""
        if not self._frames:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self._frames)

    @property
    def frame_count(self) -> int:
        return len(self._frames)

    @property
    def has_speech(self) -> bool:
        return self._has_speech


class BargeDetector:
    """打断检测：连续 3 块（0.3s）有语音即判定；静音重置（对齐 play_file_with_barge_in）。"""

    def __init__(self, threshold: float = None):
        self._threshold = VAD_THRESHOLD if threshold is None else threshold
        self._streak = 0

    def feed(self, samples: np.ndarray) -> bool:
        level = float(np.abs(samples).mean())
        if level > self._threshold:
            self._streak += 1
            return self._streak >= BARGE_STREAK
        self._streak = 0
        return False


# ── 会话（actor 模型）───────────────────────────────────────

async def _safe_send_json(ws: WebSocket, payload: dict):
    try:
        await asyncio.wait_for(ws.send_json(payload), SEND_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise _SendAborted("发送超时（客户端未读取）") from e


async def _safe_send_bytes(ws: WebSocket, data: bytes):
    try:
        await asyncio.wait_for(ws.send_bytes(data), SEND_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise _SendAborted("发送超时（客户端未读取）") from e


class _Session:
    """单个连接的会话状态。音频帧与控制消息由 rx_task 路由，事件驱动状态机。"""

    def __init__(self, deps: VoiceDeps):
        self.deps = deps
        self.state = "IDLE"          # IDLE | LISTENING | REPLYING
        self.voice = ""              # 本连接音色（空 = 用全局 SERVER_VOICE）
        self.client = ""             # 本连接是什么客户端（"mobile"/"pet"/"cli"/""）
        self.closed = False
        self._kws_stream = None      # 仅 rx_task 访问（IDLE 喂帧）
        self._kws_cooldown_until = 0.0
        self._reject_cooldown_until = 0.0  # 声纹拒绝冷却，防连续拒绝
        self._collector: Optional[UtteranceCollector] = None
        self._barge = BargeDetector()
        self._barge_mute_until = 0.0     # 起播静默窗（见 BARGE_GRACE_SEC）
        self._last_audio_sent = 0.0      # 上一句音频是什么时候发的（诊断用）
        self._pending_reset = False
        self.reply_outcome = ("done", "")
        self.barged = False
        # 连续对话：回复完免唤醒继续听下一句（客户端开关控制，默认关）
        self.continuous = False
        # 电平自适应：记录最近 2 秒的电平，唤醒时校准 VAD 阈值（说话轻的人也能检测）
        self._level_ring: list = []
        self._vad_threshold = VAD_THRESHOLD
        # 声纹锁定：音频环形缓冲（最近 4 秒）+ 会话级登记表
        self._audio_ring: list = []
        self._owner_emb = None       # 主人的声纹向量（会话内录入，重置/重连后重新录入）
        self._enrolled = False
        # 事件（rx_task 置位；actor 主循环消费）
        self.wake_event = asyncio.Event()
        self.text_event = asyncio.Event()
        self.reset_event = asyncio.Event()
        self.listen_event = asyncio.Event()
        self.utterance_done = asyncio.Event()
        self.barge_event = asyncio.Event()
        self.stop_event = asyncio.Event()
        self._pending_text = ""

    def _voice(self) -> str:
        return self.voice or (self.deps.get_voice() if self.deps.get_voice else "zh-CN-XiaoyiNeural")

    def _calibrate_vad(self) -> float:
        """按唤醒词前后 2 秒的电平校准 VAD 阈值：电平低则降阈值，范围 [基础/4, 基础]。"""
        if not self._level_ring:
            return VAD_THRESHOLD
        wake_level = max(self._level_ring)
        return max(VAD_THRESHOLD / 4, min(VAD_THRESHOLD, wake_level / 2))

    def _speaker_check(self, samples: np.ndarray) -> bool:
        """声纹锁定校验：未录入则录入；已录入则比对。返回 True 表示通过（或未启用）。

        三点原则（都是踩过坑之后加的）：
        1. **先掐静音再提声纹** —— 手上是"唤醒词前后 4 秒"，真正的语音可能只有 1 秒；
        2. **算不出/太短就放行** —— 宁可漏认，也别把主人挡在门外（拒绝一次要等 5 秒冷却）；
        3. **高分通过时把这次并进声纹中心** —— 用得越多越准，低分的不并，免得被带偏。
        """
        deps = self.deps
        if deps.speaker is None:
            return True
        try:
            embedding = deps.speaker.embed(samples)
        except Exception as e:
            print(f"[声纹] 提取失败，跳过锁定: {e}", flush=True)
            return True
        if embedding is None:
            print("[声纹] 有效语音太短，本次跳过校验（放行）", flush=True)
            return True

        if not self._enrolled:
            self._owner_emb = embedding
            self._enrolled = True
            print("[声纹] 已录入主人声音（下次唤醒开始校验）", flush=True)
            return True

        try:
            score = deps.speaker.similarity(self._owner_emb, embedding)
        except Exception as e:
            print(f"[声纹] 比对失败，跳过锁定: {e}", flush=True)
            return True
        ok = score >= deps.speaker.threshold
        print(f"[声纹] 相似度 {score:.3f}（阈值 {deps.speaker.threshold:.2f}）→ "
              f"{'通过' if ok else '拒绝'}", flush=True)
        if ok and score >= ADAPT_THRESHOLD:
            # 高分通过：并进声纹中心（0.7 旧 + 0.3 新），让模板越用越贴近主人
            self._owner_emb = [
                0.7 * a + 0.3 * b for a, b in zip(self._owner_emb, embedding)
            ]
        return ok

    def _push_audio_ring(self, samples: np.ndarray):
        self._audio_ring.append(samples)
        if len(self._audio_ring) > 40:  # 保留 4 秒
            self._audio_ring.pop(0)

    async def feed(self, samples: np.ndarray, ws: WebSocket = None):
        """rx_task 调用：按会话状态路由音频。"""
        level = float(np.abs(samples).mean())
        self._level_ring.append(level)
        if len(self._level_ring) > 20:  # 保留 2 秒
            self._level_ring.pop(0)
        if self.state == "IDLE":
            if self._kws_stream is None or self.deps.kws is None:
                return
            self._push_audio_ring(samples)
            async with self.deps.kws_lock:
                keyword = self.deps.kws.feed(self._kws_stream, samples)
            if keyword and time.time() >= self._kws_cooldown_until:
                # 拒绝冷却：只用来"别重复刷提示"，**绝不能跳过校验** ——
                # 早先这里是直接 return，结果旁人说一句就把主人一起挡在门外 5 秒；
                # 旁边要是一直有声音（电视、聊天），冷却不停续期，主人就永远唤不醒。
                # 校验本身只要 20~50ms，每次照跑完全划得来。
                in_reject_cooldown = time.time() < self._reject_cooldown_until
                # 声纹锁定：用唤醒词前后 4 秒音频录入/比对，用后清空避免混入旧音频
                wake_audio = (
                    np.concatenate(self._audio_ring)
                    if self._audio_ring
                    else np.zeros(1600, dtype=np.float32)
                )
                self._audio_ring.clear()
                if self.deps.speaker_lock is not None:
                    async with self.deps.speaker_lock:
                        ok = await asyncio.to_thread(self._speaker_check, wake_audio)
                else:
                    ok = await asyncio.to_thread(self._speaker_check, wake_audio)
                if not ok:
                    self._reject_cooldown_until = time.time() + REJECT_COOLDOWN_SEC
                    if not in_reject_cooldown:
                        print("[声纹] 声音不匹配，已忽略（主人仍可随时唤醒）", flush=True)
                        if ws is not None:
                            await _safe_send_json(
                                ws, {"type": "speaker_reject", "text": "声音不是主人，已忽略"}
                            )
                    return
                # 通过：才进入唤醒冷却（主人紧接着再喊也能立即响应）
                self._kws_cooldown_until = time.time() + KWS_COOLDOWN_SEC
                self._vad_threshold = self._calibrate_vad()
                self.wake_event.set()
        elif self.state == "LISTENING":
            if self._collector is not None and self._collector.feed(samples):
                self.utterance_done.set()
        elif self.state == "REPLYING":
            # 起播静默窗：刚发完音频那一下自己的外放最响，先别听（见 BARGE_GRACE_SEC）
            if time.time() < self._barge_mute_until:
                return
            level = float(np.abs(samples).mean())
            if self._barge.feed(samples):
                # 打出真实数字，好判断到底是什么把它打断的：
                # 距上次发音频很近（<1s）→ 几乎是自己的回声；隔得远 → 才是环境杂音
                since = time.time() - self._last_audio_sent if self._last_audio_sent else -1
                print(f"[打断] 电平 {level:.4f} > 门限 {self._vad_threshold * BARGE_MULT:.4f}"
                      f"（聆听门 {self._vad_threshold:.4f}，距上次发音频 {since:.2f}s）", flush=True)
                self.barge_event.set()

    async def handle_control(self, ctrl: dict, ws: WebSocket):
        """rx_task 调用：处理 JSON 控制消息。"""
        t = ctrl.get("type")
        if t == "hello":
            self.voice = str(ctrl.get("voice") or "")
            self.client = str(ctrl.get("client") or "")
            await _safe_send_json(ws, {"type": "hello_ok", "voice": self._voice()})
        elif t == "text":
            self._pending_text = str(ctrl.get("text") or "").strip()
            if self._pending_text:
                self.text_event.set()
        elif t == "stop":
            self.stop_event.set()
        elif t == "listen":
            # 单击宠物等显式动作：免唤醒词直接聆听（仅待机接受，声纹锁不拦显式授权）
            if self.state == "IDLE":
                self.listen_event.set()
        elif t == "continuous":
            # 连续对话开关：回复完接着听，不用每句喊「小音」
            self.continuous = bool(ctrl.get("enabled"))
            await _safe_send_json(ws, {"type": "continuous", "enabled": self.continuous})
        elif t == "reset":
            self._pending_reset = True
            self.reset_event.set()


async def _wait_any(s: _Session, events: dict, timeout: float = None) -> str:
    """等待任一事件（或连接关闭/超时）；返回触发的事件名或 "timeout"。"""
    tasks = {name: asyncio.create_task(ev.wait()) for name, ev in events.items()}
    try:
        done, _ = await asyncio.wait(
            tasks.values(), return_when=asyncio.FIRST_COMPLETED, timeout=timeout
        )
        for name, t in tasks.items():
            if t in done:
                return name
        return "timeout"
    finally:
        for t in tasks.values():
            t.cancel()


async def _do_reset(ws: WebSocket, s: _Session):
    deps = s.deps
    async with deps.turn_lock:
        deps.bot.reset()
        clear_conversation()
        deps.history.clear()
        pop_latest_turn(deps.queue)
    s.reset_event.clear()
    s._pending_reset = False
    # 重新录入声纹：重置后下一次唤醒视为新主人
    s._owner_emb = None
    s._enrolled = False
    await _safe_send_json(ws, {"type": "status", "text": "🔄 对话已重置"})
    deps.queue.put_nowait(
        {"sentences": [], "status": "🔄 对话已重置", "history": list(deps.history)}
    )


async def _reply_loop(ws: WebSocket, s: _Session, user_text: str):
    """REPLYING 主体（在 TURN_LOCK 内运行）：LLM 流式 → 逐句发文本+mp3。可被取消。"""
    deps = s.deps
    parts: list[str] = []
    seq = 0
    try:
        async for sentence in sentence_stream(deps.bot.chat_stream(user_text)):
            seq += 1
            parts.append(sentence)
            deps.history[-1]["content"] = "🤖 " + "".join(parts)
            if deps.bot.status:
                await _safe_send_json(ws, {"type": "status", "text": deps.bot.status})
            # seq 让客户端把"这句文字"和"这段配音"对上号：合成比朗读慢时文字会跑到声音前面，
            # 客户端拿 seq 就能等配音开始播了再上屏（否则聊天记录永远快半拍）
            await _safe_send_json(ws, {"type": "sentence", "text": sentence, "seq": seq})
            try:
                mp3 = await deps.tts.synthesize_stream(sentence)
            except Exception:
                continue  # 合成失败：跳过播报，文字已显示
            await _safe_send_json(ws, {"type": "audio", "format": "mp3", "seq": seq})
            await _safe_send_bytes(ws, mp3)
            s._last_audio_sent = time.time()
            s._barge_mute_until = s._last_audio_sent + BARGE_GRACE_SEC
        full_reply = "".join(parts)
        if not full_reply:
            deps.history[-1]["content"] = "🤖 （暂无回复）"
            s.reply_outcome = ("done", "⚠️ 模型返回空回复")
        else:
            s.reply_outcome = ("done", "✅ 已完成回复")
    except asyncio.CancelledError:
        # 打断/停止：手动补录部分回复，保持上下文完整（镜像 main.py:117-121）
        if parts:
            deps.history[-1]["content"] = "🤖 " + "".join(parts)
        else:
            deps.history[-1]["content"] = "🤖 （已打断）"
        s.reply_outcome = ("stopped", "⏹ 已停止播报")
        raise
    except _SendAborted as e:
        deps.history[-1]["content"] = f"🤖 {''.join(parts)}" if parts else "🤖 （回复中断）"
        s.reply_outcome = ("error", f"⚠️ {e}")
    except Exception as e:
        logger.exception("回复轮次失败")          # 完整体现在 xiaoyin.log 里
        deps.history[-1]["content"] = f"⚠️ LLM 调用失败：{e}"
        s.reply_outcome = ("error", f"⚠️ 回复失败：{type(e).__name__} {str(e)[:60]}")


async def _reply_phase(ws: WebSocket, s: _Session, user_text: str, prefix: str):
    """REPLYING：发射回复任务，与打断/停止事件竞争。返回后由调用方决定去向。"""
    deps = s.deps
    deps.history.append({"role": "user", "content": f"{prefix} {user_text}"})
    deps.history.append({"role": "assistant", "content": ""})
    deps.tts.VOICE = s._voice()
    # 本轮请求来自哪类客户端：必须在进轮次锁之前拷出来（锁内所有会话共用同一个 bot，
    # 分不清谁是谁）；工具靠它决定「点歌是推给手机还是播在电脑上」
    import devices

    devices.set_turn_origin(s.client)
    deps.queue.put_nowait(
        {"sentences": [], "status": f"🤖 已识别：「{user_text}」— 正在生成回复...",
         "history": list(deps.history)}
    )
    s.state = "REPLYING"
    s.reply_outcome = ("done", "")
    s.barged = False
    s._barge = BargeDetector(threshold=s._vad_threshold * BARGE_MULT)
    s.barge_event.clear()
    s.stop_event.clear()

    async with deps.turn_lock:
        reply_task = asyncio.create_task(_reply_loop(ws, s, user_text))
        barge_wait = asyncio.create_task(s.barge_event.wait())
        stop_wait = asyncio.create_task(s.stop_event.wait())
        done, pending = await asyncio.wait(
            {reply_task, barge_wait, stop_wait}, return_when=asyncio.FIRST_COMPLETED
        )
        barged = barge_wait in done
        if reply_task not in done:
            reply_task.cancel()
            await asyncio.gather(reply_task, return_exceptions=True)
        for t in (barge_wait, stop_wait):
            if t not in done:
                t.cancel()
            else:
                await t

    stopped = s.reply_outcome[0] != "done"
    save_conversation(deps.history, deps.bot.conversation)
    if barged:
        await _safe_send_json(ws, {"type": "barge_in"})
    await _safe_send_json(ws, {"type": "turn_end", "status": s.reply_outcome[1], "stopped": stopped})
    deps.queue.put_nowait(
        {"sentences": [], "status": s.reply_outcome[1], "history": list(deps.history)}
    )
    return barged


async def _listen_rounds(ws: WebSocket, s: _Session) -> str:
    """首轮聆听 + 连续模式下的续听轮次，直到不再续听或连接断开。

    首轮用宽松超时（等用户开口）；续听轮用 CONTINUOUS_TIMEOUT（默认 10s）——
    静一会儿就说明聊完了，回待机等下次唤醒。
    """
    result = await _listening_phase(ws, s)
    while should_follow_up(s.continuous, result) and not s.closed:
        await _safe_send_json(
            ws, {"type": "status", "text": "🔁 还在听，直接说下一句（停顿一会儿我就先歇着）"}
        )
        result = await _listening_phase(
            ws, s, timeout=CONTINUOUS_TIMEOUT, follow_up=True
        )
    return result


async def _listening_phase(ws: WebSocket, s: _Session,
                           timeout: float = LISTEN_NO_SPEECH_TIMEOUT,
                           follow_up: bool = False):
    """LISTENING：采集话语 → STT → 回复。返回后状态由调用方设置。

    timeout：等用户开口的上限。唤醒后的首轮宽松（15s），连续对话的续听轮次紧一些
    （默认 10s，.env 可调），免得以为人走了还在那干等。
    follow_up：本轮的结束语要区分——续听轮超时说明用户聊完了，不该再说
    「说「小音」再试一次」。
    """
    deps = s.deps
    s.state = "LISTENING"
    s._collector = UtteranceCollector(threshold=s._vad_threshold)
    s.utterance_done.clear()
    trigger = await _wait_any(
        s, {"done": s.utterance_done, "reset": s.reset_event},
        timeout=timeout,
    )
    if s.closed:
        return "closed"
    if trigger == "reset":
        await _do_reset(ws, s)
        return "reset"

    audio = s._collector.take()
    s._collector = None

    if trigger == "timeout":
        # 一直没听到声音：给用户明确反馈，回待机。
        # 续听轮超时说明用户聊完了，措辞要区分开
        await _safe_send_json(ws, {"type": "status", "text": (
            "👂 连续对话结束，说「小音」再叫我" if follow_up
            else "👂 没有听到声音，说「小音」再试一次"
        )})
        return "timeout"

    if len(audio) < int(SAMPLE_RATE * UTTERANCE_MIN_SEC):
        await _safe_send_json(
            ws, {"type": "status", "text": "⚠️ 语音太短没听清，说「小音」再试一次"}
        )
        return "too_short"  # 回 IDLE 继续监听

    # 即时反馈：STT 要 1~3 秒，先告知用户「正在识别」降低等待感
    await _safe_send_json(ws, {"type": "status", "text": "🎤 正在识别…"})
    async with deps.stt_lock:
        user_text = await asyncio.to_thread(deps.stt.transcribe, audio, SAMPLE_RATE)
    if s.closed:
        return "closed"
    if not user_text:
        await _safe_send_json(
            ws, {"type": "status", "text": "⚠️ 没识别到内容，说「小音」再试一次"}
        )
        return "empty"

    # 记一笔识别质量：时长 + 峰值电平 + 识别文本。
    # "识别不准"这类反馈光看文字没法判断是模型问题还是麦克风太小声/削顶，
    # 有了峰值就能看出来（正常说话大约 0.05~0.3；贴着 1.0 说明削顶失真，小于 0.02 说明太小声）
    peak = float(np.abs(audio).max()) if len(audio) else 0.0
    print(f"[识别] {len(audio) / SAMPLE_RATE:.1f}s 峰值{peak:.3f} → 「{user_text}」", flush=True)
    await _safe_send_json(ws, {"type": "transcript", "text": user_text})
    barged = await _reply_phase(ws, s, user_text, "🎤")
    return "barged" if barged else "done"


async def _session(ws: WebSocket, deps: VoiceDeps):
    """单连接会话主循环。"""
    s = _Session(deps)
    kws_ok = deps.kws is not None
    if kws_ok:
        s._kws_stream = deps.kws.create_stream()
    await ws.accept()
    await _safe_send_json(ws, {"type": "ready", "sample_rate": SAMPLE_RATE, "chunk": CHUNK, "kws": kws_ok})

    async def _rx_loop():
        buf = b""
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") != "websocket.receive":
                    break
                if msg.get("bytes") is not None:
                    buf += msg["bytes"]
                    if len(buf) > MAX_BUFFER_BYTES:
                        buf = b""
                        continue
                    frames, buf = iter_frames(buf)
                    for frame in frames:
                        samples = (
                            np.frombuffer(frame, dtype="<i2").astype(np.float32) / 32768.0
                        )
                        await s.feed(samples, ws)
                elif msg.get("text") is not None:
                    try:
                        await s.handle_control(json.loads(msg["text"]), ws)
                    except (ValueError, TypeError):
                        pass
        finally:
            s.closed = True
            for ev in (s.wake_event, s.text_event, s.reset_event, s.listen_event,
                       s.utterance_done, s.barge_event, s.stop_event):
                ev.set()

    rx_task = asyncio.create_task(_rx_loop())
    try:
        while not s.closed:
            if s._pending_reset:
                await _do_reset(ws, s)
                continue
            if s.state != "IDLE":
                s.state = "IDLE"
                continue
            # IDLE：等唤醒 / 文字 / 点击 / 重置
            s.wake_event.clear()
            s.text_event.clear()
            s.reset_event.clear()
            s.listen_event.clear()
            trigger = await _wait_any(s, {
                "wake": s.wake_event, "text": s.text_event,
                "reset": s.reset_event, "listen": s.listen_event,
            })
            if s.closed:
                break
            if trigger == "reset":
                continue  # 循环顶部处理 _pending_reset
            if trigger == "listen":
                # 显式点击：跳过唤醒词与声纹，直接聆听
                print(f"[语音会话] 点击唤醒", flush=True)
                await _safe_send_json(
                    ws, {"type": "wake", "keyword": "小音", "source": "click"}
                )
                await _listen_rounds(ws, s)
                s.state = "IDLE"
                continue
            if trigger == "text":
                text = s._pending_text
                s._pending_text = ""
                if not text:
                    continue
                await _safe_send_json(ws, {"type": "transcript", "text": text})
                await _reply_phase(ws, s, text, "⌨️")
                s.state = "IDLE"
                continue
            # wake
            print(f"[语音会话] 检测到唤醒词", flush=True)
            await _safe_send_json(ws, {"type": "wake", "keyword": "小音"})
            # 打断后免唤醒继续听是原本就有的行为；连续模式把它扩展到"正常回复完也继续"
            await _listen_rounds(ws, s)
            s.state = "IDLE"
    except Exception:
        pass
    finally:
        s.closed = True
        rx_task.cancel()
        try:
            await ws.close()
        except Exception:
            pass


def register_routes(app, deps: VoiceDeps):
    """在 FastAPI app 上注册 /ws/assistant（须在 launch 前调用）。"""

    async def _ws(ws: WebSocket):
        if deps.kws is None and deps.stt is None and deps.bot is None:
            await ws.accept()
            await ws.send_json({"type": "error", "error": "语音服务未初始化"})
            await ws.close()
            return
        await _session(ws, deps)

    app.add_api_websocket_route(WS_PATH, _ws)


DEVICE_WS_PATH = "/ws/device"


def register_device_routes(app):
    """安卓遥控 App 的常驻连接：服务器往它推「播放/暂停/切歌」指令。

    与语音会话不同，这条连接没有音频上下行，只做下行指令投递（登记进 devices 注册表，
    音乐工具推指令时按需查表）。
    """
    import devices

    async def _ws(ws: WebSocket):
        await ws.accept()
        devices.register(ws)
        try:
            await _safe_send_json(ws, {"type": "ready", "path": DEVICE_WS_PATH})
            while True:
                msg = await ws.receive()
                if msg.get("type") != "websocket.receive":
                    break                      # 断开帧
                if msg.get("text"):
                    try:
                        ctrl = json.loads(msg["text"])
                    except ValueError:
                        continue
                    if ctrl.get("type") == "hello":
                        await _safe_send_json(ws, {"type": "hello_ok", "client": "device"})
                    elif ctrl.get("type") == "result":
                        # 指令执行回执：交给 devices 去唤醒等它的那次调用
                        # （没有这个的话，"手机上没切成"会被当场说成"切好了"）
                        devices.resolve_result(ctrl)
                    # 其余上行消息忽略即可
        except Exception:
            pass
        finally:
            devices.remove(ws)
            try:
                await ws.close()
            except Exception:
                pass

    app.add_api_websocket_route(DEVICE_WS_PATH, _ws)
