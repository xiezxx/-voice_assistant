"""大语言模型模块：通过 DeepSeek API 进行对话理解和生成（支持 Function Calling）。"""

from openai import AsyncOpenAI
from config import Config
from tools import TOOL_SCHEMAS, TOOL_DISPLAY, execute_tool, get_due_reminders_text


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
        system = self._system_content()

        try:
            response = await self.client.chat.completions.create(
                model=Config.DEEPSEEK_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": system},
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
        system = self._system_content()

        full_reply = ""
        try:
            # ── 第一轮：流式请求，边产出文本边收集工具调用 ──
            stream = await self.client.chat.completions.create(
                model=Config.DEEPSEEK_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": system},
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
                model=Config.DEEPSEEK_MODEL,
                max_tokens=512,
                messages=[
                    {"role": "system", "content": system},
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

    def _system_content(self) -> str:
        """系统提示词 + 到点日程提醒（若有；一次性标记，避免每轮重复提醒）。"""
        try:
            reminder = get_due_reminders_text()
        except Exception:
            reminder = ""
        return Config.SYSTEM_PROMPT + (("\n\n" + reminder) if reminder else "")

    def _build_messages(self) -> list[dict]:
        """构建 API 消息列表：过滤无效消息，并把工具调用整组配平。

        工具调用必须是「assistant(tool_calls) 紧跟它要的那些 tool 结果」这样成对的，
        否则 DeepSeek 直接 400。而 {@code conversation[-30:]} 是从中间截断的，两种残组都会出现：
        窗口正好从一条 tool 消息开始（孤儿 tool）、或 assistant 的调用被截断在窗口外。
        所以这里按顺序走一遍，凑不齐的整组一起丢掉 —— 不然一旦畸形就是**永久**失败：
        400 → 回滚 → 上下文原样 → 下次还是同一条消息打头 → 再 400。
        """
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

        out: list[dict] = []
        group_at = -1            # 当前这组工具调用在 out 里的起点
        expected: list[str] = []  # 还等着哪些 tool_call_id
        for m in messages:
            if m["role"] == "tool":
                if expected and m["tool_call_id"] == expected[0]:
                    expected.pop(0)
                    out.append(m)
                continue                      # 没轮到它 → 孤儿，丢掉
            if expected:                      # 上一组没凑齐就来了别的消息 → 整组作废
                del out[group_at:]
                expected = []
            out.append(m)
            if m["role"] == "assistant" and m.get("tool_calls"):
                expected = [tc["id"] for tc in m["tool_calls"]]
                group_at = len(out) - 1
        if expected:                          # 结尾还欠着 tool 结果 → 整组作废
            del out[group_at:]
        return out
