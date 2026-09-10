"""唤醒词检测：说「小音」唤醒助手。

双实现，自动选择：
1. AsrWakeWordListener（默认，零配置）：待机时用能量门检测说话，交给 Whisper
   快速识别，文本含"小音"即唤醒。响应约 1 秒，无需任何账号。
2. PicovoiceWakeWordListener（可选）：配置 PICOVOICE_ACCESS_KEY 与 .ppn 模型后
   自动启用，低延迟、低功耗。

配置缺失时优雅降级，不影响其他功能。
"""

import os
import time

import numpy as np
import sounddevice as sd

from config import Config

# 唤醒词（含常见同音字，防止 Whisper 听写差异）
_WAKE_PHRASES = ("小音", "晓音", "小英", "小颖")
# 说话能量阈值与判定参数
_SPEECH_THRESHOLD = Config.VAD_THRESHOLD
_SPEECH_STREAK = 3          # 连续 0.3 秒有能量才判定开始说话
_UTTERANCE_MAX_SEC = 2.5    # 待机语音最长录制时长
_UTTERANCE_SILENCE_SEC = 0.8  # 尾静音判定


def is_wake_phrase(text: str) -> bool:
    """判断识别文本是否包含唤醒词（容忍同音字与空格）。"""
    cleaned = (text or "").replace(" ", "").replace("，", "")
    return any(p in cleaned for p in _WAKE_PHRASES)


class AsrWakeWordListener:
    """基于现有 Whisper 的唤醒词检测：能量门 → 短录音 → 识别 → 关键词匹配。"""

    def __init__(self, stt):
        self.stt = stt
        self.sample_rate = Config.SAMPLE_RATE
        self._stream = None

    def _ensure_stream(self):
        if self._stream is None:
            input_device = int(os.getenv("AUDIO_INPUT_DEVICE", "-1"))
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="float32",
                device=input_device if input_device >= 0 else None,
                blocksize=int(self.sample_rate * 0.1),
            )
            self._stream.start()

    def _read_level(self) -> float:
        """读取 0.1 秒音频，返回平均电平。"""
        chunk, _ = self._stream.read(int(self.sample_rate * 0.1))
        return float(np.abs(chunk).mean())

    def wait_for_wake_word(self, timeout: float = None) -> bool:
        """阻塞监听唤醒词；timeout 秒内未检测到返回 False。"""
        self._ensure_stream()
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if deadline is not None and time.time() >= deadline:
                return False

            # 1. 能量门：等有人开始说话
            streak = 0
            speech_started = False
            while True:
                if deadline is not None and time.time() >= deadline:
                    return False
                if self._read_level() > _SPEECH_THRESHOLD:
                    streak += 1
                    if streak >= _SPEECH_STREAK:
                        speech_started = True
                        break
                else:
                    streak = 0
            if not speech_started:
                continue

            # 2. 录制一小段（最长 2.5 秒，尾静音 0.8 秒结束）
            frames = []
            silence = 0
            max_frames = int(_UTTERANCE_MAX_SEC * 10)
            silence_frames = int(_UTTERANCE_SILENCE_SEC * 10)
            while len(frames) < max_frames:
                chunk, _ = self._stream.read(int(self.sample_rate * 0.1))
                frames.append(chunk)
                if float(np.abs(chunk).mean()) < _SPEECH_THRESHOLD:
                    silence += 1
                    if silence >= silence_frames and len(frames) > silence_frames:
                        break
                else:
                    silence = 0
            if len(frames) < 4:  # 不足 0.4 秒视为误触
                continue

            # 3. 识别并匹配唤醒词
            audio = np.concatenate(frames).flatten()
            try:
                text = self.stt.transcribe(audio, sample_rate=self.sample_rate)
            except Exception:
                continue
            if is_wake_phrase(text):
                print(f"[唤醒词] 识别到: {text.strip()}", flush=True)
                return True

    def close(self):
        """释放麦克风（下一轮对话录音前必须调用）。"""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


class PicovoiceWakeWordListener:
    """基于 Picovoice Porcupine 的低延迟唤醒（需要 AccessKey + .ppn 模型）。"""

    def __init__(self):
        import pvporcupine

        self._porcupine = pvporcupine.create(
            access_key=Config.PICOVOICE_ACCESS_KEY,
            keyword_paths=[Config.WAKE_WORD_MODEL_PATH],
            sensitivities=[Config.WAKE_WORD_SENSITIVITY],
        )
        self.sample_rate = self._porcupine.sample_rate
        self.frame_length = self._porcupine.frame_length
        input_device = int(os.getenv("AUDIO_INPUT_DEVICE", "-1"))
        self.input_device = input_device if input_device >= 0 else None
        self._stream = None

    def _ensure_stream(self):
        if self._stream is None:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype="int16",
                device=self.input_device,
                blocksize=self.frame_length,
            )
            self._stream.start()

    def wait_for_wake_word(self, timeout: float = None) -> bool:
        self._ensure_stream()
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if deadline is not None and time.time() >= deadline:
                return False
            data, _ = self._stream.read(self.frame_length)
            pcm = data.flatten().astype(np.int16).tolist()
            if self._porcupine.process(pcm) >= 0:
                return True

    def close(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._porcupine.delete()


def picovoice_available() -> tuple[bool, str]:
    """Picovoice 版唤醒词是否可用。"""
    from pathlib import Path

    if not Config.PICOVOICE_ACCESS_KEY:
        return False, "未配置 PICOVOICE_ACCESS_KEY"
    if not Path(Config.WAKE_WORD_MODEL_PATH).exists():
        return False, "未找到 .ppn 唤醒词模型"
    try:
        import pvporcupine  # noqa: F401
    except ImportError:
        return False, "未安装 pvporcupine"
    return True, "就绪"


def create_wake_listener(stt):
    """创建唤醒监听器：Picovoice 配置齐全时用低延迟方案，否则用 ASR 方案。

    Returns:
        (listener, mode)  mode ∈ {"porcupine", "asr"}
    """
    ok, reason = picovoice_available()
    if ok:
        return PicovoiceWakeWordListener(), "porcupine"
    return AsrWakeWordListener(stt), "asr"
