"""唤醒词检测：基于 Picovoice Porcupine 的免提唤醒。

唤醒词为「小音，小音」（自定义模型，需在 Picovoice 控制台训练后下载 .ppn 文件）。
未配置 AccessKey 或模型文件时，通过 wakeword_available() 优雅降级为手动触发。
"""

import os
from pathlib import Path

import numpy as np
import sounddevice as sd

from config import Config


def wakeword_available() -> tuple[bool, str]:
    """检查唤醒词功能是否可用，返回 (是否可用, 原因)。"""
    if not Config.PICOVOICE_ACCESS_KEY:
        return False, "未配置 PICOVOICE_ACCESS_KEY（.env）"
    model = Path(Config.WAKE_WORD_MODEL_PATH)
    if not model.exists():
        return False, f"未找到唤醒词模型 {model.name}（请放入 models/ 目录）"
    try:
        import pvporcupine  # noqa: F401
    except ImportError:
        return False, "未安装 pvporcupine（pip install pvporcupine）"
    return True, "就绪"


class WakeWordListener:
    """持续监听麦克风，检测到唤醒词返回。"""

    def __init__(self):
        import pvporcupine

        self._porcupine = pvporcupine.create(
            access_key=Config.PICOVOICE_ACCESS_KEY,
            keyword_paths=[Config.WAKE_WORD_MODEL_PATH],
            sensitivities=[Config.WAKE_WORD_SENSITIVITY],
        )
        self.sample_rate = self._porcupine.sample_rate      # 16000
        self.frame_length = self._porcupine.frame_length    # 512
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
        """阻塞监听唤醒词。

        Args:
            timeout: 单次监听最长秒数；None 表示一直等到唤醒。

        Returns:
            True 表示检测到唤醒词，False 表示超时未检测到。
        """
        import time

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
        """释放音频流与模型资源。"""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._porcupine.delete()
