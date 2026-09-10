# -*- coding: utf-8 -*-
"""流式 TTS 与打断功能的离线测试（不联网、不发声）。"""

import asyncio


async def _collect(agen):
    return [x async for x in agen]


async def chunks():
    for c in ["你好，", "我是小音。", "今天天气", "不错！", "再见"]:
        yield c


def test_sentence_stream():
    from speech_utils import sentence_stream
    result = asyncio.run(_collect(sentence_stream(chunks())))
    expect = ["你好，我是小音。", "今天天气不错！", "再见"]
    assert result == expect, f"切分结果不符: {result}"
    print("✓ 句子切分:", result)


def test_comma_split():
    """长句应在逗号处提前切开，降低首响延迟。"""
    from speech_utils import sentence_stream

    async def long_chunks():
        # 一个 40+ 字、无句号的长句，只有逗号
        yield "根据劳动法规定，用人单位需要按时足额支付工资，"
        yield "不得无故克扣或者拖欠，否则需要承担相应的法律责任"

    result = asyncio.run(_collect(sentence_stream(long_chunks())))
    assert len(result) >= 2, f"长句应被逗号切开: {result}"
    assert all(len(s) >= 18 for s in result[:-1]), f"切分过碎: {result}"
    print("✓ 长句逗号细分:", result)


def test_conversation_store():
    """会话持久化：保存后能完整恢复，清空后读取为空。"""
    from conversation_store import save_conversation, load_conversation, clear_conversation

    clear_conversation()
    assert load_conversation() == ([], [])
    history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
    conv = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好呀"}]
    save_conversation(history, conv)
    h, c = load_conversation()
    assert h == history and c == conv, (h, c)
    print("✓ 会话保存/恢复")
    clear_conversation()
    assert load_conversation() == ([], [])
    print("✓ 会话清空")


def test_audio_duration_fallback():
    from speech_utils import audio_duration_sec
    d = audio_duration_sec(r"C:\不存在\fake.mp3", text_len=10)
    assert 2.5 < d < 4.0, f"兜底时长异常: {d}"
    print(f"✓ 音频时长兜底估算: {d:.2f}s")


def test_chat_stream_early_break():
    """打断场景：提前中断流式生成，上下文不含未完成的助手回复。"""
    from llm import ChatBot

    bot = ChatBot()

    class FakeCompletions:
        async def create(self, **kwargs):
            class Delta:
                content = "你好"
                tool_calls = None
            class Choice:
                delta = Delta()
            class Chunk:
                choices = [Choice()]

            async def gen():
                while True:  # 无限流
                    yield Chunk()
            return gen()

    class FakeClient:
        @property
        def chat(self):
            return self
        completions = FakeCompletions()

    bot.client = FakeClient()

    async def run():
        got = []
        async for c in bot.chat_stream("测试"):
            got.append(c)
            if len(got) >= 3:
                break  # 模拟打断：提前终止
        return got, bot.conversation

    got, conv = asyncio.run(run())
    assert len(got) == 3
    # 提前终止：用户消息还在，助手回复未落库（由调用方手动补录部分回复）
    assert len(conv) == 1 and conv[0]["role"] == "user", f"上下文异常: {conv}"
    print("✓ 提前中断后上下文干净:", conv)


def test_process_text_streaming():
    """Gradio 处理函数：假 bot + 假 tts，验证逐句播报流程。"""
    import time
    import app  # 会加载 Whisper 模型（已缓存）

    class FakeBot:
        status = ""  # 与真实 ChatBot 接口保持一致
        conversation = []  # 供 save_conversation 读取

        async def chat_stream(self, user_text):
            yield "你好，"
            yield "我是小音。"
            yield "今天天气不错！"

    class FakeTTS:
        VOICE = ""
        async def synthesize(self, text, output_path=None):
            await asyncio.sleep(0.02)
            return r"C:\不存在\fake.mp3"

    app.bot = FakeBot()
    app.tts = FakeTTS()

    async def run():
        yields = []
        history = []
        t0 = time.time()
        async for h, status, audio in app.process_text("你好", history, "zh-CN-XiaoyiNeural"):
            yields.append((status, audio))
        elapsed = time.time() - t0
        return yields, history, elapsed

    yields, history, elapsed = asyncio.run(run())
    audio_yields = [y for y in yields if y[1]]
    assert len(audio_yields) == 2, f"应播报 2 句，实际 {len(audio_yields)}"
    assert history[-1]["content"] == "🤖 你好，我是小音。今天天气不错！", history[-1]
    assert yields[-1][0].startswith("✅"), yields[-1]
    print(f"✓ Web 流式播报: {len(audio_yields)} 句音频, 总耗时 {elapsed:.1f}s")
    for s, a in yields:
        print(f"    {s}  audio={'有' if a else '无'}")


if __name__ == "__main__":
    test_sentence_stream()
    test_comma_split()
    test_audio_duration_fallback()
    test_chat_stream_early_break()
    test_conversation_store()
    test_process_text_streaming()
    print("\n全部通过 ✅")
