"""音频采集与播放工具。"""

import os
import numpy as np
import sounddevice as sd
from config import Config


def list_devices():
    """列出所有音频设备，方便调试。"""
    print("\n可用音频设备：")
    for i, dev in enumerate(sd.query_devices()):
        io = ""
        if dev["max_input_channels"] > 0:
            io += " [输入]"
        if dev["max_output_channels"] > 0:
            io += " [输出]"
        print(f"  {i}: {dev['name']}{io} (采样率: {dev['default_samplerate']:.0f}Hz)")


class AudioRecorder:
    """基于 sounddevice 的录音器，支持实时电平检测。"""

    def __init__(self):
        self.sample_rate = Config.SAMPLE_RATE
        self.channels = Config.CHANNELS
        self.device = int(os.getenv("AUDIO_INPUT_DEVICE", "-1"))
        if self.device < 0:
            self.device = None  # 使用系统默认

    def record_until_silence(
        self,
        threshold: float = None,
        silence_duration: float = None,
        max_duration: float = 15.0,
    ) -> np.ndarray | None:
        """录音直到检测到静音，返回 float32 numpy 数组（-1.0 ~ 1.0）。"""
        if threshold is None:
            threshold = Config.VAD_THRESHOLD
        if silence_duration is None:
            silence_duration = Config.SILENCE_DURATION

        frames: list[np.ndarray] = []
        silence_frames = 0
        frames_per_check = int(self.sample_rate * 0.1)
        silence_threshold_frames = int(silence_duration * self.sample_rate / frames_per_check)

        print("🎤 正在听...", end="", flush=True)

        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
            device=self.device,
        )
        stream.start()

        try:
            while len(frames) * frames_per_check < max_duration * self.sample_rate:
                chunk, _ = stream.read(frames_per_check)
                chunk = chunk.flatten()
                frames.append(chunk)

                level = float(np.abs(chunk).mean())
                if level < threshold:
                    silence_frames += 1
                else:
                    silence_frames = 0
                    if len(frames) % 5 == 0:
                        print(".", end="", flush=True)

                if silence_frames >= silence_threshold_frames and len(frames) > silence_threshold_frames:
                    break
        finally:
            stream.stop()
            stream.close()

        audio = np.concatenate(frames)
        duration = len(audio) / self.sample_rate
        print(f" ({duration:.1f}s)")

        if duration < 0.3:
            print("[提示] 未检测到有效语音")
            return None

        return audio

    def record_fixed(self, duration: float = 5.0) -> np.ndarray:
        """录制固定时长的音频。"""
        print(f"🎤 录制 {duration} 秒...", end="", flush=True)

        audio = sd.rec(
            int(duration * self.sample_rate),
            samplerate=self.sample_rate,
            channels=self.channels,
            dtype="float32",
        )
        sd.wait()

        print(" 完成")
        return audio.flatten()


class AudioPlayer:
    """音频播放器，用于播放 TTS 生成的语音。"""

    def play(self, audio: np.ndarray, sample_rate: int = None):
        """播放 numpy 音频数组。"""
        if sample_rate is None:
            sample_rate = Config.SAMPLE_RATE
        sd.play(audio, samplerate=sample_rate)
        sd.wait()

    def play_file(self, filepath: str):
        """播放音频文件（使用 pygame 支持 MP3/WAV 等格式）。"""
        import pygame

        pygame.mixer.init(frequency=Config.SAMPLE_RATE)
        try:
            pygame.mixer.music.load(filepath)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy():
                pygame.time.Clock().tick(10)
        finally:
            pygame.mixer.quit()
