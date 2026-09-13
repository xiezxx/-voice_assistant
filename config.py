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
    # Whisper 模型下载源：国内直连 huggingface.co 不通（实测超时），默认走镜像。
    # 想用官方源或自建镜像，在 .env 里设 HF_ENDPOINT 覆盖。
    HF_ENDPOINT = os.getenv("HF_ENDPOINT", "https://hf-mirror.com")

    VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.02"))
    SILENCE_DURATION = float(os.getenv("SILENCE_DURATION", "0.8"))

    # 唤醒词「小音」：sherpa-onnx KWS 方案（默认，本地模型）
    WAKE_WORD_ENABLED = os.getenv("WAKE_WORD_ENABLED", "1") == "1"
    KWS_MODEL_DIR = os.getenv(
        "KWS_MODEL_DIR", str(Path(PROJECT_ROOT) / "models" / "kws-wenetspeech")
    )
    WAKE_KEYWORDS_FILE = os.getenv(
        "WAKE_KEYWORDS_FILE", str(Path(PROJECT_ROOT) / "models" / "wake_keywords.txt")
    )
    KWS_KEYWORDS_SCORE = float(os.getenv("KWS_KEYWORDS_SCORE", "1.0"))
    KWS_KEYWORDS_THRESHOLD = float(os.getenv("KWS_KEYWORDS_THRESHOLD", "0.25"))
    # 可选 Picovoice 低延迟方案（需 AccessKey + .ppn 模型）
    PICOVOICE_ACCESS_KEY = os.getenv("PICOVOICE_ACCESS_KEY", "")
    WAKE_WORD_MODEL_PATH = os.getenv(
        "WAKE_WORD_MODEL_PATH",
        str(Path(PROJECT_ROOT) / "models" / "xiao-yin-xiao-yin_zh_windows_v3_0_0.ppn"),
    )
    WAKE_WORD_SENSITIVITY = float(os.getenv("WAKE_WORD_SENSITIVITY", "0.5"))

    SAMPLE_RATE = 16000
    CHANNELS = 1

    # 快递查询（快递鸟免费接口，可选）：https://www.kdniao.com 注册后申请即时查询API
    KDNIAO_EBUSINESS_ID = os.getenv("KDNIAO_EBUSINESS_ID", "")
    KDNIAO_APP_KEY = os.getenv("KDNIAO_APP_KEY", "")

    # 声纹锁定：识别并记忆主人的声音，其他人说「小音」会被忽略
    # 首次唤醒录入声纹；重置对话/重连后重新录入
    SPEAKER_LOCK = os.getenv("SPEAKER_LOCK", "1") == "1"
    SPEAKER_MODEL_PATH = os.getenv(
        "SPEAKER_MODEL_PATH",
        str(Path(PROJECT_ROOT) / "models" / "speech_campplus_sv_zh_en_16k-common_advanced.onnx"),
    )
    # 声纹匹配阈值（余弦相似度 0~1，越大越严格；误拒绝多就调低）
    SPEAKER_THRESHOLD = float(os.getenv("SPEAKER_THRESHOLD", "0.6"))

    SYSTEM_PROMPT = (
        "你是一个友好的 AI 语音助手，名叫小音。"
        "请用简洁、口语化的中文回复，每次回复控制在2-3句话以内。"
        "语气要自然，像朋友聊天一样。"
        "你可以帮用户查天气、空气质量、时间、汇率、新闻，做计算，"
        "设置/查询/取消日程提醒，以及查快递物流。"
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
