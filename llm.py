"""大语言模型模块：通过 DeepSeek API 进行对话理解和生成。"""

from openai import AsyncOpenAI
from config import Config


class ChatBot:
    """封装 DeepSeek API，保持对话上下文。"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=Config.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
        )
        self.conversation: list[dict] = []

    def reset(self):
        """重置对话上下文。"""
        self.conversation = []

    async def chat(self, user_text: str) -> str:
        """发送消息给 DeepSeek，返回完整回复文本。"""
        if not user_text or not user_text.strip():
            return "我没有听清你说的话，可以再说一遍吗？"

        self.conversation.append({"role": "user", "content": user_text})

        try:
            response = await self.client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": Config.SYSTEM_PROMPT},
                    *self._build_messages(),
                ],
            )
        except Exception as e:
            # 请求失败时回滚用户消息，避免上下文污染
            self.conversation.pop()
            raise RuntimeError(f"DeepSeek API 调用失败: {e}") from e

        reply = response.choices[0].message.content
        if reply is None:
            reply = "抱歉，我暂时无法回复，请稍后再试。"

        # 只保存有效回复
        self.conversation.append({"role": "assistant", "content": reply})
        return reply

    def chat_sync(self, user_text: str) -> str:
        """同步版本的 chat，方便在非异步环境（如 Gradio）中调用。"""
        import asyncio
        return asyncio.run(self.chat(user_text))

    async def chat_stream(self, user_text: str):
        """流式对话：逐步 yield 文本片段。"""
        self.conversation.append({"role": "user", "content": user_text})

        full_reply = ""
        try:
            stream = await self.client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": Config.SYSTEM_PROMPT},
                    *self._build_messages(),
                ],
                stream=True,
            )

            async for chunk in stream:
                if chunk.choices[0].delta.content:
                    text = chunk.choices[0].delta.content
                    full_reply += text
                    yield text
        except Exception as e:
            # 请求失败时回滚用户消息，避免上下文污染
            self.conversation.pop()
            raise RuntimeError(f"DeepSeek API 调用失败: {e}") from e

        # 只保存有效回复
        if full_reply:
            self.conversation.append({"role": "assistant", "content": full_reply})

    def _build_messages(self) -> list[dict]:
        """构建 API 消息列表，过滤无效消息防止格式错误。"""
        messages = []
        for msg in self.conversation[-20:]:
            role = msg.get("role", "")
            content = msg.get("content", "")
            # 跳过无效消息
            if not role or not content:
                continue
            if role not in ("user", "assistant", "system"):
                continue
            messages.append({"role": role, "content": str(content)})
        return messages
