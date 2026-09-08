"""语音识别模块（Speech-to-Text）：基于 Faster-Whisper 的本地语音转文字。"""

import numpy as np
from scipy.signal import resample
from config import Config


class SpeechRecognizer:
    """封装 Faster-Whisper，支持多语言语音识别。"""

    def __init__(self):
        self.model = None

    def load(self):
        """加载 Whisper 模型（首次运行会下载，约 150MB~3GB 取决于模型大小）。"""
        from faster_whisper import WhisperModel

        print(f"[STT] 加载 Whisper 模型: {Config.WHISPER_MODEL} ({Config.WHISPER_DEVICE})...")
        self.model = WhisperModel(
            Config.WHISPER_MODEL,
            device=Config.WHISPER_DEVICE,
            compute_type=Config.WHISPER_COMPUTE_TYPE,
        )
        print("[STT] 模型加载完成")

    def transcribe(self, audio: np.ndarray, sample_rate: int = None) -> str:
        """将音频转为文字，返回识别结果。

        Args:
            audio: 一维 float32 音频数组（支持立体声，自动转单声道）。
            sample_rate: 音频采样率。如果与 Whisper 期望的 16kHz 不同，会自动重采样。
        """
        if self.model is None:
            self.load()

        if sample_rate is None:
            sample_rate = Config.SAMPLE_RATE

        # 立体声 → 单声道
        if audio.ndim == 2:
            audio = audio.mean(axis=1)

        # 重采样到 16kHz（Whisper 要求）
        if sample_rate != Config.SAMPLE_RATE:
            target_len = int(len(audio) * Config.SAMPLE_RATE / sample_rate)
            audio = resample(audio, target_len)

        # 归一化
        audio = audio.astype(np.float32)
        if np.abs(audio).max() > 1.0:
            audio = audio / np.abs(audio).max()

        segments, info = self.model.transcribe(
            audio,
            beam_size=5,
            language="zh",
            vad_filter=True,
        )

        text_parts = []
        for segment in segments:
            text_parts.append(segment.text.strip())

        result = "".join(text_parts)
        return result if result else ""
