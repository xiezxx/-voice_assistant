"""配置管理：从 .env 文件和环境变量加载配置。"""

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent
load_dotenv(PROJECT_ROOT / ".env")


class Config:
    """全局配置，从环境变量读取，有合理默认值。"""

    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", os.getenv("ANTHROPIC_API_KEY", ""))
    WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")
    WHISPER_DEVICE = os.getenv("WHISPER_DEVICE", "cpu")
    WHISPER_COMPUTE_TYPE = os.getenv("WHISPER_COMPUTE_TYPE", "int8")

    VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.02"))
    SILENCE_DURATION = float(os.getenv("SILENCE_DURATION", "1.0"))

    SAMPLE_RATE = 16000
    CHANNELS = 1

    SYSTEM_PROMPT = (
        "你是一个友好的 AI 语音助手，名叫小音。"
        "请用简洁、口语化的中文回复，每次回复控制在2-3句话以内。"
        "语气要自然，像朋友聊天一样。"
    )

    @classmethod
    def validate(cls) -> bool:
        """检查必要配置是否齐全。"""
        if not cls.DEEPSEEK_API_KEY:
            print("[错误] 请在 .env 文件中设置 DEEPSEEK_API_KEY")
            print("  1. 复制 .env.example 为 .env")
            print("  2. 填入你的 DeepSeek API Key")
            return False
        return True
