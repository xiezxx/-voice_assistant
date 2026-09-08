"""语音合成模块（Text-to-Speech）：基于 Edge-TTS 的文字转语音。"""

import tempfile
import asyncio
from pathlib import Path


class SpeechSynthesizer:
    """封装 Edge-TTS，免费、中文自然度高。"""

    # 可选音色：https://github.com/rany2/edge-tts
    VOICE = "zh-CN-XiaoyiNeural"  # 中文女声，自然亲切

    @staticmethod
    def list_voices():
        """列出可用的中文音色。"""
        import edge_tts

        async def _list():
            voices = await edge_tts.list_voices()
            for v in voices:
                if "zh" in v["Locale"]:
                    print(f"  {v['ShortName']}: {v['Locale']} - {v['FriendlyName']}")

        asyncio.run(_list())

    async def synthesize(self, text: str, output_path: str = None) -> str:
        """将文本合成为语音，返回音频文件路径。"""
        import edge_tts

        if output_path is None:
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            output_path = tmp.name
            tmp.close()

        communicate = edge_tts.Communicate(text, self.VOICE)
        await communicate.save(output_path)

        return output_path

    def synthesize_sync(self, text: str, output_path: str = None) -> str:
        """同步版本的 synthesize，方便在 Gradio 中调用。"""
        import asyncio
        return asyncio.run(self.synthesize(text, output_path))

    async def synthesize_stream(self, text: str) -> bytes:
        """流式合成，返回完整音频字节。适合边生成 LLM 文本边合成。"""
        import edge_tts

        communicate = edge_tts.Communicate(text, self.VOICE)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)
