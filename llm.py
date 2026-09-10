"""大语言模型模块：通过 DeepSeek API 进行对话理解和生成（支持 Function Calling）。"""

from openai import AsyncOpenAI
from config import Config
from tools import TOOL_SCHEMAS, TOOL_DISPLAY, execute_tool


class ChatBot:
    """封装 DeepSeek API，保持对话上下文，需要时自动调用工具。"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=Config.DEEPSEEK_API_KEY,
            base_url="https://api.deepseek.com",
        )
        self.conversation: list[dict] = []
        self.status: str = ""  # 当前工具调用状态（供界面展示）

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
        """流式对话：逐步 yield 文本片段；模型需要时自动调用工具。

        流程：
        1. 第一轮流式请求：正常对话边生成边产出；若模型返回 tool_calls，
           则记录调用并执行工具（天气/时间/计算），期间 self.status 供界面展示
        2. 第二轮请求：携带工具结果生成最终回答，分块 yield 保持流式观感
        """
        self.conversation.append({"role": "user", "content": user_text})
        self.status = ""

        full_reply = ""
        try:
            # ── 第一轮：流式请求，边产出文本边收集工具调用 ──
            stream = await self.client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": Config.SYSTEM_PROMPT},
                    *self._build_messages(),
                ],
                tools=TOOL_SCHEMAS,
                stream=True,
            )

            tool_calls: dict[int, dict] = {}
            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_reply += delta.content
                    yield delta.content
                for tc in delta.tool_calls or []:
                    item = tool_calls.setdefault(
                        tc.index, {"id": "", "name": "", "arguments": ""}
                    )
                    if tc.id:
                        item["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            item["name"] += tc.function.name
                        if tc.function.arguments:
                            item["arguments"] += tc.function.arguments

            if not tool_calls:
                # 普通对话，直接收尾
                if full_reply:
                    self.conversation.append({"role": "assistant", "content": full_reply})
                return

            # ── 执行工具 ──
            self.conversation.append({
                "role": "assistant",
                "content": full_reply or None,
                "tool_calls": [
                    {
                        "id": item["id"],
                        "type": "function",
                        "function": {"name": item["name"], "arguments": item["arguments"]},
                    }
                    for item in tool_calls.values()
                ],
            })
            for item in tool_calls.values():
                self.status = f"🔧 正在{TOOL_DISPLAY.get(item['name'], item['name'])}..."
                result = await execute_tool(item["name"], item["arguments"])
                self.conversation.append({
                    "role": "tool",
                    "tool_call_id": item["id"],
                    "content": result,
                })
            self.status = ""

            # ── 第二轮：携带工具结果生成最终回答 ──
            response = await self.client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=512,
                messages=[
                    {"role": "system", "content": Config.SYSTEM_PROMPT},
                    *self._build_messages(),
                ],
            )
            full_reply = response.choices[0].message.content or ""
            for i in range(0, len(full_reply), 24):
                yield full_reply[i : i + 24]
            if full_reply:
                self.conversation.append({"role": "assistant", "content": full_reply})
        except Exception as e:
            self.status = ""
            # 回滚本轮新增消息（工具结果 / 工具调用 / 用户消息），避免上下文污染
            while self.conversation and self.conversation[-1]["role"] != "user":
                self.conversation.pop()
            if self.conversation and self.conversation[-1]["role"] == "user":
                self.conversation.pop()
            raise RuntimeError(f"DeepSeek API 调用失败: {e}") from e

    def _build_messages(self) -> list[dict]:
        """构建 API 消息列表：过滤无效消息，剔除工具结果缺失的残缺调用。"""
        messages = []
        for msg in self.conversation[-30:]:
            role = msg.get("role", "")
            if role not in ("user", "assistant", "tool"):
                continue
            content = msg.get("content")
            if role != "assistant" and not content:
                continue
            item = {"role": role, "content": str(content or "")}
            if role == "assistant" and msg.get("tool_calls"):
                item["tool_calls"] = msg["tool_calls"]
            if role == "tool":
                item["tool_call_id"] = msg.get("tool_call_id", "")
            messages.append(item)

        # 若 assistant 的工具调用缺少对应 tool 结果（如被打断），丢弃该调用防止 API 报错
        tool_ids = {m["tool_call_id"] for m in messages if m["role"] == "tool"}
        cleaned = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                if not all(tc["id"] in tool_ids for tc in m["tool_calls"]):
                    continue
            cleaned.append(m)
        return cleaned
