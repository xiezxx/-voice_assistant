package com.xiaoyin.remote.local;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 组装发给大模型的对话历史。逐行照搬 {@code llm.py} 的 {@code _build_messages}。
 *
 * <p><b>为什么这个类值得单独存在、还值得测：</b>工具调用必须是
 * 「assistant(带 tool_calls) 紧跟它要的那几条 tool 结果」严格成对，否则 API 直接 400。
 * 而历史窗口是 {@code conversation[-30:]}，**从中间截断天然会切出残组**（孤儿 tool 消息、
 * 或被截掉结果的裸调用）。更糟的是一旦畸形就<b>永久</b>失败：400 → 回滚 → 上下文恢复原样
 * → 窗口起点不变 → 下一轮还是同一条消息打头 → 再 400。
 *
 * <p>电脑端为此写过一次修复（提交 682d5e8），这里是把那次修复原样搬到手机上 ——
 * 端侧要是漏了这段，会在真机上重演一模一样的死循环。
 *
 * <p>纯逻辑、零 Android 依赖，可在电脑上 javac 编译后与 Python 原件逐组比对。
 */
public final class MessageList {

    /** 一次工具调用。 */
    public static final class ToolCall {
        public String id;
        public String name;
        public String arguments;

        public ToolCall() {
        }

        public ToolCall(String id, String name, String arguments) {
            this.id = id;
            this.name = name;
            this.arguments = arguments;
        }
    }

    /** 一条消息。role ∈ user / assistant / tool。 */
    public static final class Msg {
        public String role;
        public String content;
        /** 只有 assistant 带工具调用时才非空 */
        public List<ToolCall> toolCalls;
        /** 只有 role=tool 才有：回应的是哪一次调用 */
        public String toolCallId;

        public Msg() {
        }

        public static Msg user(String text) {
            Msg m = new Msg();
            m.role = "user";
            m.content = text;
            return m;
        }

        public static Msg assistant(String text) {
            Msg m = new Msg();
            m.role = "assistant";
            m.content = text;
            return m;
        }

        public static Msg assistantToolCalls(List<ToolCall> calls) {
            Msg m = new Msg();
            m.role = "assistant";
            m.toolCalls = calls;
            return m;
        }

        public static Msg tool(String toolCallId, String content) {
            Msg m = new Msg();
            m.role = "tool";
            m.toolCallId = toolCallId;
            m.content = content;
            return m;
        }
    }

    /** 历史窗口：和电脑端一致，只发最近 30 条 */
    public static final int WINDOW = 30;

    private MessageList() {
    }

    /** 组装出可直接发给 API 的消息列表（已配平工具调用组）。 */
    public static List<Msg> build(List<Msg> conversation) {
        int from = Math.max(0, conversation.size() - WINDOW);
        List<Msg> window = conversation.subList(from, conversation.size());

        // 第一遍：过滤掉角色不认识的、以及非 assistant 但内容为空的
        List<Msg> items = new ArrayList<>();
        for (Msg msg : window) {
            String role = msg.role == null ? "" : msg.role;
            if (!role.equals("user") && !role.equals("assistant") && !role.equals("tool")) {
                continue;
            }
            if (!role.equals("assistant") && (msg.content == null || msg.content.isEmpty())) {
                continue;
            }
            items.add(msg);
        }

        // 第二遍：按顺序把工具调用整组配平
        List<Msg> out = new ArrayList<>();
        int groupAt = -1;                 // 当前这组工具调用在 out 里的起点
        List<String> expected = new ArrayList<>();   // 还等着哪些 tool_call_id

        for (Msg m : items) {
            if ("tool".equals(m.role)) {
                if (!expected.isEmpty() && expected.get(0).equals(m.toolCallId)) {
                    expected.remove(0);
                    out.add(m);
                }
                // 没轮到它 → 孤儿消息，丢掉（API 会 400）
                continue;
            }
            if (!expected.isEmpty()) {
                // 上一组还没凑齐就来了别的消息 → 整组作废
                while (out.size() > groupAt) {
                    out.remove(out.size() - 1);
                }
                expected.clear();
            }
            out.add(m);
            if ("assistant".equals(m.role) && m.toolCalls != null && !m.toolCalls.isEmpty()) {
                expected = new ArrayList<>();
                for (ToolCall tc : m.toolCalls) {
                    expected.add(tc.id);
                }
                groupAt = out.size() - 1;
            }
        }
        if (!expected.isEmpty()) {        // 结尾还欠着 tool 结果 → 整组作废
            while (out.size() > groupAt) {
                out.remove(out.size() - 1);
            }
        }
        return out;
    }

    /** 给自测用：把消息列表压成一行紧凑文本，方便和 Python 侧逐组比对。 */
    public static String describe(List<Msg> msgs) {
        List<String> parts = new ArrayList<>();
        for (Msg m : msgs) {
            StringBuilder sb = new StringBuilder(m.role);
            if (m.toolCalls != null && !m.toolCalls.isEmpty()) {
                List<String> ids = new ArrayList<>();
                for (ToolCall tc : m.toolCalls) {
                    ids.add(tc.id);
                }
                Collections.sort(ids);
                sb.append("(calls:").append(String.join(",", ids)).append(")");
            }
            if ("tool".equals(m.role)) {
                sb.append("(").append(m.toolCallId).append(")");
            }
            parts.add(sb.toString());
        }
        // 用 join 而不是"每条后面补一个分隔符"：后者会多出个尾巴，和 Python 的 join 对不上
        return String.join(" | ", parts);
    }
}
