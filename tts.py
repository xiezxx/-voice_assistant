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

    async def _synthesize_once(self, text: str) -> bytes:
        """一次合成（一条新连接）。返回空音频视为失败，交给上层重试。"""
        import edge_tts

        communicate = edge_tts.Communicate(text, self.VOICE)
        chunks = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                chunks.append(chunk["data"])
        data = b"".join(chunks)
        if not data:
            raise RuntimeError("edge-tts 返回了空音频")
        return data

    async def synthesize_stream(self, text: str, hedge_after: float = 3.0,
                                deadline: float = 12.0) -> bytes:
        """流式合成，返回完整音频字节。适合边生成 LLM 文本边合成。

        每句都要新建一条到微软的连接，国内偶尔会卡住十几秒 —— 实测同一句话
        单独测只要 1.5~3 秒，卡的是连接而不是内容。所以超过 {@code hedge_after}
        还没回来就**另起一条并发重试，谁先回来用谁**：把偶发的十几秒压到几秒。
        正常情况（3 秒内返回）不会有任何额外开销。

        {@code deadline} 是兜底：万一两条都挂着不返回也不报错，到点就放弃并抛错
        （调用方会跳过这句的播报）——总比一直干等、整轮回复卡死强。
        """
        loop = asyncio.get_event_loop()
        first = asyncio.ensure_future(self._synthesize_once(text))
        done, _ = await asyncio.wait({first}, timeout=hedge_after)
        if done:
            return first.result()

        started = loop.time()
        second = asyncio.ensure_future(self._synthesize_once(text))
        pending = {first, second}
        last_err: Exception | None = None
        try:
            while pending:
                left = deadline - (loop.time() - started)
                if left <= 0:
                    break
                done, pending = await asyncio.wait(
                    pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    try:
                        return task.result()
                    except Exception as e:      # 这条失败了，等另一条
                        last_err = e
        finally:
            for task in (first, second):
                if not task.done():
                    task.cancel()
        raise last_err or TimeoutError(f"edge-tts 超过 {deadline:.0f}s 没返回")
