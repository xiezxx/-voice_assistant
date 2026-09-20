package com.xiaoyin.remote.local;

import org.json.JSONArray;
import org.json.JSONObject;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.TimeUnit;

import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okhttp3.Call;

/**
 * 端侧大模型客户端：直接连 DeepSeek（手机自己发请求，不经过电脑）。
 *
 * <p><b>严格对齐电脑端的 {@code llm.py}</b>，因为那边踩过的坑这边一模一样会再踩一遍：
 * <ol>
 *   <li>第一轮带 {@code tools} 且 {@code stream=true}：文字边收边切句，工具调用按
 *       {@code index} 分片聚合（分片到达，name/arguments 要<b>字符串拼接</b>）；</li>
 *   <li>有工具调用 → 先补一条 assistant(带 tool_calls)，再把每个结果作为
 *       {@code {"role":"tool","tool_call_id":...}} 回填，**顺序不能乱**；</li>
 *   <li>第二轮**非流式**取完整回复，再按 24 字切片"伪装流式"（保持打字机观感）；</li>
 *   <li>历史要用 {@link MessageList} 配平工具调用组 —— 少这一步，窗口一截断就永久 400
 *       （电脑端为此修过一次，见提交 682d5e8）。</li>
 * </ol>
 *
 * <p>阻塞式：调用方在轮次线程里跑；{@link #cancel()} 会中断连接，
 * 阻塞中的 read 抛 IOException 自然退出（打断就是这么实现的）。
 */
public class LlmClient {

    public interface Sink {
        /** 攒够一句了，可以拿去合成播报 */
        void onSentence(String sentence);

        /** 工具调用状态（界面状态栏显示「🔧 正在查询时间...」） */
        void onStatus(String status);

        /** 整轮结束（整段回复文本） */
        void onDone(String fullReply);
    }

    private static final String API_URL = "https://api.deepseek.com/chat/completions";
    /** 默认模型；实际用哪个由 {@link #setModel} 决定（界面上可改，改完下一句就生效） */
    public static final String DEFAULT_MODEL = "deepseek-chat";
    private volatile String model = DEFAULT_MODEL;
    private static final int MAX_TOKENS = 512;
    /** 历史窗口，和电脑端一致 */
    private static final int WINDOW = MessageList.WINDOW;
    /** 第二轮非流式回复的切片大小（对齐 llm.py 的 24 字） */
    private static final int FAKE_STREAM_CHUNK = 24;

    private static final String SYSTEM_PROMPT =
            "你是一个友好的 AI 语音助手，名叫小音。请用简洁、口语化的中文回复，每次回复控制在2-3句话以内。"
                    + "语气要自然，像朋友聊天一样。你可以帮用户查时间、做计算，"
                    + "还可以用 QQ 音乐放歌和控制播放。";

    private final String apiKey;
    private final LocalTools tools;
    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)     // 流式：两个 chunk 之间最多等 60s
            .writeTimeout(15, TimeUnit.SECONDS)
            .build();

    /** 对话历史（含工具调用，跨轮保持） */
    private final List<MessageList.Msg> conversation = new ArrayList<>();
    private volatile Call current;

    public LlmClient(String apiKey, LocalTools tools) {
        this.apiKey = apiKey == null ? "" : apiKey.trim();
        this.tools = tools;
    }

    public boolean hasKey() {
        return !apiKey.isEmpty();
    }

    /**
     * 换模型。**每次请求都会读一次**，所以改完下一句就生效，不用重启语音助手。
     *
     * <p>可用值随账号而变（比如 {@code deepseek-chat} / {@code deepseek-flash} /
     * {@code deepseek-v4-pro}），填错了会在界面上看到 HTTP 400 的原文。
     */
    public void setModel(String m) {
        if (m != null && !m.trim().isEmpty()) {
            model = m.trim();
        }
    }

    public String model() {
        return model;
    }

    public void reset() {
        conversation.clear();
    }

    /** 打断用：掐断当前请求，阻塞中的读会抛 IOException */
    public void cancel() {
        Call c = current;
        if (c != null) {
            c.cancel();
        }
    }

    /**
     * 跑一轮对话（阻塞）。整句通过 {@link Sink#onSentence} 边生成边吐出来。
     */
    public void stream(String userText, SpeechSegmenter segmenter, Sink sink) throws IOException {
        conversation.add(MessageList.Msg.user(userText));
        String full = "";
        try {
            // ── 第一轮：流式 + 工具 ──
            JSONObject body = baseBody(true);
            Response first = post(body, true);
            if (first == null) {
                throw new IOException("请求失败");
            }

            StringBuilder reply = new StringBuilder();
            List<MessageList.ToolCall> calls = new ArrayList<>();
            readStream(first, reply, calls, segmenter, sink);
            full = reply.toString();

            if (calls.isEmpty()) {
                if (full.length() > 0) {
                    conversation.add(MessageList.Msg.assistant(full));
                }
                sink.onDone(full);
                return;
            }

            // ── 有工具调用：先补 assistant，再逐个回填结果 ──
            conversation.add(MessageList.Msg.assistantToolCalls(calls));
            for (MessageList.ToolCall tc : calls) {
                sink.onStatus("🔧 正在" + LocalTools.display(tc.name) + "...");
                JSONObject args = new JSONObject();
                try {
                    args = new JSONObject(tc.arguments == null || tc.arguments.isEmpty() ? "{}" : tc.arguments);
                } catch (Exception ignored) {
                    // 参数不是合法 JSON：当空参处理，工具自己会兜底
                }
                String result = tools.execute(tc.name, args);
                conversation.add(MessageList.Msg.tool(tc.id, result));
            }
            sink.onStatus("");

            // ── 第二轮：非流式，拿到完整回复再按 24 字切片假装流式 ──
            JSONObject second = baseBody(false);
            String text = postText(second);
            if (text == null) {
                throw new IOException("第二轮请求失败");
            }
            for (int i = 0; i < text.length(); i += FAKE_STREAM_CHUNK) {
                String piece = text.substring(i, Math.min(text.length(), i + FAKE_STREAM_CHUNK));
                for (String s : segmenter.feed(piece)) {
                    sink.onSentence(s);
                }
            }
            String tail = segmenter.flush();
            if (!tail.isEmpty()) {
                sink.onSentence(tail);
            }
            if (!text.isEmpty()) {
                conversation.add(MessageList.Msg.assistant(text));
            }
            sink.onDone(text);
        } catch (Exception e) {
            // 回滚本轮新加的消息，别把半截历史留给下一轮（和 llm.py 的做法一致）
            while (!conversation.isEmpty()
                    && !"user".equals(conversation.get(conversation.size() - 1).role)) {
                conversation.remove(conversation.size() - 1);
            }
            if (!conversation.isEmpty()) {
                conversation.remove(conversation.size() - 1);
            }
            if (e instanceof IOException) {
                throw (IOException) e;
            }
            throw new IOException(e.getMessage() == null ? e.toString() : e.getMessage(), e);
        }
    }

    // ── 请求体与 HTTP ───────────────────────────────────────

    private JSONObject baseBody(boolean stream) throws IOException {
        try {
            JSONObject body = new JSONObject();
            body.put("model", model);
            body.put("max_tokens", MAX_TOKENS);
            body.put("stream", stream);

            JSONArray messages = new JSONArray();
            messages.put(new JSONObject().put("role", "system").put("content", SYSTEM_PROMPT));
            for (MessageList.Msg m : MessageList.build(conversation)) {
                JSONObject o = new JSONObject();
                o.put("role", m.role);
                o.put("content", m.content == null ? JSONObject.NULL : m.content);
                if (m.toolCalls != null && !m.toolCalls.isEmpty()) {
                    JSONArray arr = new JSONArray();
                    for (MessageList.ToolCall tc : m.toolCalls) {
                        arr.put(new JSONObject()
                                .put("id", tc.id)
                                .put("type", "function")
                                .put("function", new JSONObject()
                                        .put("name", tc.name)
                                        .put("arguments", tc.arguments == null ? "{}" : tc.arguments)));
                    }
                    o.put("tool_calls", arr);
                }
                if (m.toolCallId != null) {
                    o.put("tool_call_id", m.toolCallId);
                }
                messages.put(o);
            }
            body.put("messages", messages);
            if (stream) {
                body.put("tools", LocalTools.schemas());
            }
            return body;
        } catch (Exception e) {
            throw new IOException("构造请求失败: " + e.getMessage(), e);
        }
    }

    private Response post(JSONObject body, boolean stream) throws IOException {
        Request req = new Request.Builder()
                .url(API_URL)
                .header("Authorization", "Bearer " + apiKey)
                .header("Content-Type", "application/json")
                .post(RequestBody.create(body.toString(), MediaType.parse("application/json")))
                .build();
        Call call = http.newCall(req);
        current = call;
        Response resp = call.execute();
        if (!resp.isSuccessful()) {
            String err = "";
            try {
                ResponseBody b = resp.body();
                err = b == null ? "" : b.string();
            } catch (Exception ignored) {
            }
            resp.close();
            throw new IOException("HTTP " + resp.code() + " " + err.substring(0, Math.min(200, err.length())));
        }
        return resp;
    }

    /** 非流式请求，取 message.content */
    private String postText(JSONObject body) throws IOException {
        try (Response resp = post(body, false)) {
            ResponseBody b = resp.body();
            if (b == null) {
                return null;
            }
            JSONObject json = new JSONObject(b.string());
            JSONArray choices = json.optJSONArray("choices");
            if (choices == null || choices.length() == 0) {
                return null;
            }
            JSONObject msg = choices.getJSONObject(0).optJSONObject("message");
            return msg == null ? null : msg.optString("content", "");
        } catch (org.json.JSONException e) {
            throw new IOException("解析回复失败: " + e.getMessage(), e);
        }
    }

    /**
     * 读 SSE 流：边收边切句，同时把 tool_calls 分片聚合起来。
     */
    private void readStream(Response resp, StringBuilder reply,
                            List<MessageList.ToolCall> calls,
                            SpeechSegmenter segmenter, Sink sink) throws IOException {
        // 工具调用的分片：index → {id,name,arguments}
        java.util.Map<Integer, MessageList.ToolCall> acc = new java.util.TreeMap<>();
        try (ResponseBody body = resp.body()) {
            if (body == null) {
                return;
            }
            BufferedReader reader = new BufferedReader(
                    new InputStreamReader(body.byteStream(), StandardCharsets.UTF_8));
            String line;
            while ((line = reader.readLine()) != null) {
                if (!line.startsWith("data:")) {
                    continue;
                }
                String data = line.substring(5).trim();
                if (data.isEmpty() || "[DONE]".equals(data)) {
                    continue;
                }
                try {
                    JSONObject chunk = new JSONObject(data);
                    JSONArray choices = chunk.optJSONArray("choices");
                    if (choices == null || choices.length() == 0) {
                        continue;
                    }
                    JSONObject delta = choices.getJSONObject(0).optJSONObject("delta");
                    if (delta == null) {
                        continue;
                    }
                    String content = delta.optString("content", "");
                    if (!content.isEmpty()) {
                        reply.append(content);
                        for (String s : segmenter.feed(content)) {
                            sink.onSentence(s);
                        }
                    }
                    JSONArray tcs = delta.optJSONArray("tool_calls");
                    if (tcs != null) {
                        for (int i = 0; i < tcs.length(); i++) {
                            JSONObject tc = tcs.getJSONObject(i);
                            int idx = tc.optInt("index", 0);
                            MessageList.ToolCall item = acc.get(idx);
                            if (item == null) {
                                item = new MessageList.ToolCall("", "", "");
                                acc.put(idx, item);
                            }
                            String id = tc.optString("id", "");
                            if (!id.isEmpty()) {
                                item.id = id;
                            }
                            JSONObject fn = tc.optJSONObject("function");
                            if (fn != null) {
                                item.name += fn.optString("name", "");      // 分片到达，要拼接
                                item.arguments += fn.optString("arguments", "");
                            }
                        }
                    }
                } catch (Exception ignored) {
                    // 单个 chunk 坏掉不该炸掉整条流（网络抖动、半截包都可能）
                }
            }
        }
        calls.addAll(acc.values());
    }
}
