"""流式语音播报辅助工具。

- sentence_stream：把 LLM 的流式文本按句切分，逐句产出
- audio_duration_sec：读取音频时长，读取失败时按中文语速估算兜底
"""

import re
from collections.abc import AsyncIterator

# 中文句子结束符（句号/问号/叹号/分号/换行）
_SENT_END = re.compile(r"[。！？!?；;\n]")
# 长句细分：缓冲区积累超过该长度时，允许在逗号处提前切开
_MIN_COMMA_SPLIT = 18
_COMMA = re.compile(r"[，,]")


async def sentence_stream(chunks: AsyncIterator[str]):
    """把流式文本切分成播报片段逐个产出，末尾残余单独产出。

    规则：
    1. 遇到句号/问号/叹号/分号/换行立即切分；
    2. 长句积累超过 18 字时，允许在逗号处提前切开（降低首响延迟）。

    例：输入 "你好，我是小音。今天天气不错！" 依次产出
        "你好，我是小音。" 和 "今天天气不错！"
    """
    buffer = ""
    async for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        while True:
            m = _SENT_END.search(buffer)
            idx = m.end() if m else None
            if idx is None:
                # 长句在逗号处提前切分：从阈值位置起找第一个逗号
                comma = _COMMA.search(buffer, _MIN_COMMA_SPLIT - 1)
                if comma:
                    idx = comma.end()
            if idx is None:
                break
            sentence = buffer[:idx].strip()
            buffer = buffer[idx:]
            if sentence:
                yield sentence
    tail = buffer.strip()
    if tail:
        yield tail


def audio_duration_sec(path: str, text_len: int = 0) -> float:
    """返回音频时长（秒）。读取失败时按中文语速估算兜底。

    text_len 用于兜底估算：Edge-TTS 中文约 0.28 秒/字，外加 0.3 秒固定开销。
    """
    try:
        import pygame

        if not pygame.mixer.get_init():
            pygame.mixer.init()
        return float(pygame.mixer.Sound(path).get_length())
    except Exception:
        return 0.3 + max(0, text_len) * 0.28
