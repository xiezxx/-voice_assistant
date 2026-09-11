"""唤醒词检测：说「小音」唤醒助手。

多实现，按优先级自动选择：
1. SherpaKwsWakeWordListener（默认）：sherpa-onnx 中文关键词检测模型（3.3M，本地、
   毫秒级），关键词在 models/wake_keywords.txt 自定义，无需训练、无需账号。
2. PicovoiceWakeWordListener（可选）：配置 AccessKey 与 .ppn 模型后启用。
3. AsrWakeWordListener（兜底）：用现有 Whisper 识别关键词，约 1 秒响应。
"""

import os
import time
from pathlib import Path

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


class SherpaKwsWakeWordListener:
    """基于 sherpa-onnx 的本地中文关键词检测（毫秒级，无需账号）。"""

    def __init__(self):
        import sherpa_onnx

        model_dir = Path(Config.KWS_MODEL_DIR)
        self._spotter = sherpa_onnx.KeywordSpotter(
            tokens=str(model_dir / "tokens.txt"),
            encoder=str(model_dir / "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            decoder=str(model_dir / "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            joiner=str(model_dir / "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
            keywords_file=Config.WAKE_KEYWORDS_FILE,
            num_threads=4,
            max_active_paths=4,
            keywords_score=Config.KWS_KEYWORDS_SCORE,
            keywords_threshold=Config.KWS_KEYWORDS_THRESHOLD,
        )
        self.sample_rate = 16000
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

    def wait_for_wake_word(self, timeout: float = None) -> bool:
        """阻塞监听唤醒词；timeout 秒内未检测到返回 False。"""
        self._ensure_stream()
        stream = self._spotter.create_stream()
        deadline = None if timeout is None else time.time() + timeout
        while True:
            if deadline is not None and time.time() >= deadline:
                return False
            chunk, _ = self._stream.read(int(self.sample_rate * 0.1))
            samples = chunk.flatten().astype(np.float32)
            stream.accept_waveform(self.sample_rate, samples)
            while self._spotter.is_ready(stream):
                self._spotter.decode_stream(stream)
            keyword = self._spotter.get_result(stream)
            if keyword:
                print(f"[唤醒词] 检测到: {keyword}", flush=True)
                return True

    def close(self):
        """释放麦克风（下一轮对话录音前必须调用）。"""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


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
    if not Config.PICOVOICE_ACCESS_KEY:
        return False, "未配置 PICOVOICE_ACCESS_KEY"
    if not Path(Config.WAKE_WORD_MODEL_PATH).exists():
        return False, "未找到 .ppn 唤醒词模型"
    try:
        import pvporcupine  # noqa: F401
    except ImportError:
        return False, "未安装 pvporcupine"
    return True, "就绪"


def sherpa_available() -> tuple[bool, str]:
    """sherpa-onnx KWS 唤醒词是否可用（本地模型 + 关键词文件齐全）。"""
    model_dir = Path(Config.KWS_MODEL_DIR)
    required = [
        "tokens.txt",
        "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
        "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
    ]
    if not all((model_dir / f).exists() for f in required):
        return False, "缺少 KWS 模型文件（models/kws-wenetspeech）"
    if not Path(Config.WAKE_KEYWORDS_FILE).exists():
        return False, "缺少唤醒词文件（models/wake_keywords.txt）"
    try:
        import sherpa_onnx  # noqa: F401
    except ImportError:
        return False, "未安装 sherpa-onnx"
    return True, "就绪"


def create_wake_listener(stt):
    """创建唤醒监听器，按优先级自动选择：sherpa-onnx > Picovoice > ASR 兜底。

    Returns:
        (listener, mode)  mode ∈ {"sherpa", "porcupine", "asr"}
    """
    ok, reason = sherpa_available()
    if ok:
        return SherpaKwsWakeWordListener(), "sherpa"
    ok, reason = picovoice_available()
    if ok:
        return PicovoiceWakeWordListener(), "porcupine"
    return AsrWakeWordListener(stt), "asr"
