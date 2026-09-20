package com.xiaoyin.remote;

import android.content.Context;
import android.media.MediaPlayer;
import android.os.Handler;
import android.os.Looper;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;
import okio.ByteString;

/**
 * 安卓版语音客户端：采集麦克风 → 推给电脑上的小音服务器 → 播放它合成的回复。
 *
 * 用的是和网页端完全相同的协议（{@code /ws/assistant}），所以服务器一行都不用改。
 * 做成原生 App 的好处是：前台服务 + 麦克风类型，**锁屏也能继续听**（浏览器做不到）。
 *
 * 客户端契约里最容易踩的三条（都按服务端实现来的，别改）：
 * <ol>
 *   <li><b>播报期间也必须继续采集</b>：服务器靠音频帧里的能量判断"用户在打断"，
 *       一暂停采集就永远打断不了；</li>
 *   <li>每帧必须是 0.1 秒（16k 下 1600 样本 / 3200 字节），否则服务器按块数算时长会全错；</li>
 *   <li>hello 必须带 {@code client:"mobile"}，否则"点歌"会被判成电脑端、播到电脑上。</li>
 * </ol>
 */
public class VoiceClient implements VoiceEngine {

    public interface Listener {
        /** 状态变化（用于界面显示和通知栏）。 */
        void onStatus(String text);

        /** 一轮对话内容：who 是 "我" 或 "小音"。界面拿它当聊天记录留下来。 */
        void onDialogue(String who, String text);

        /**
         * 会话状态，界面拿它决定状态球长什么样：
         * {@code off} / {@code idle}（在听）/ {@code wake}（听到唤醒词）/
         * {@code think}（在想）/ {@code speak}（在说）/ {@code error}。
         */
        void onState(String state);
    }

    private static final long RETRY_MS = 3000;

    private final Context context;
    private final String url;                          // ws://host:7860/ws/assistant
    private final Listener listener;
    private final OkHttpClient client;
    private final Handler handler = new Handler(Looper.getMainLooper());
    /** 采集与"发帧"：采集本身是两条链路共用的，这里只负责把帧推给服务器 */
    private final MicCapture mic;

    private WebSocket socket;
    private volatile boolean running;

    private final Deque<Clip> playQueue = new ArrayDeque<>();
    /** 还没播到的句子：seq → 文字（配音开始播时才上屏，见 onClipStart） */
    private final java.util.Map<Integer, String> pendingTexts = new java.util.TreeMap<>();
    private volatile int pendingAudioSeq = 0;   // 刚收到的 audio 事件属于哪一句
    private MediaPlayer player;
    private File currentFile;
    private boolean playing;

    public VoiceClient(Context context, String host, Listener listener) {
        this.context = context.getApplicationContext();
        this.url = "ws://" + host + "/ws/assistant";
        this.listener = listener;
        this.client = new OkHttpClient.Builder()
                .pingInterval(20, TimeUnit.SECONDS)     // 服务端每 20s Ping，OkHttp 自动回 Pong
                .build();
        this.mic = new MicCapture(new MicCapture.Listener() {
            @Override
            public void onFrame(short[] frame) {
                WebSocket ws = socket;
                if (ws != null) {
                    ws.send(ByteString.of(MicCapture.toLittleEndian(frame)));
                }
            }

            @Override
            public void onStatus(String text) {
                status(text);
            }
        });
    }

    public void start() {
        if (running) {
            return;
        }
        running = true;
        connect();
    }

    public void stop() {
        running = false;
        handler.removeCallbacksAndMessages(null);
        stopPlayback();
        stopMic();
        if (socket != null) {
            try {
                socket.close(1000, "stop");
            } catch (Exception ignored) {
            }
            socket = null;
        }
        status("语音助手已关闭");
        state("off");
    }

    /** 发一条控制消息（连续对话开关等）。 */
    public void sendControl(String json) {
        if (socket != null) {
            socket.send(json);
        }
    }

    // ── 连接 ────────────────────────────────────────────────

    private void connect() {
        if (!running) {
            return;
        }
        Request request = new Request.Builder().url(url).build();
        socket = client.newWebSocket(request, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                // client=mobile：让服务器知道"这次点歌是手机在说话"，指令才会推回手机
                webSocket.send("{\"type\":\"hello\",\"client\":\"mobile\"}");
                startMic();
                status("🎙 聆听中 — 说「小音」唤醒");
                state("idle");
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                handleEvent(text);
            }

            @Override
            public void onMessage(WebSocket webSocket, ByteString bytes) {
                // 每个二进制帧 = 一句完整 mp3（前面那个 audio 事件带着它的 seq）
                enqueueMp3(bytes.toByteArray(), pendingAudioSeq);
                pendingAudioSeq = 0;
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                stopMic();
                if (running) {
                    status("⚠️ 连接断开，重连中…");
                    state("error");
                    handler.postDelayed(VoiceClient.this::connect, RETRY_MS);
                }
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                stopMic();
                if (running) {
                    handler.postDelayed(VoiceClient.this::connect, RETRY_MS);
                }
            }
        });
    }

    private void handleEvent(String text) {
        try {
            JSONObject msg = new JSONObject(text);
            switch (msg.optString("type")) {
                case "ready":
                    status("🎙 聆听中 — 说「小音」唤醒");
                    state("idle");
                    break;
                case "wake":
                    status("🔔 唤醒成功 — 请说话");
                    state("wake");
                    break;
                case "transcript":
                    if (!msg.optString("text").isEmpty()) {
                        status("🤔 识别中：「" + msg.optString("text") + "」");
                        dialogue("我", msg.optString("text"));
                        state("think");
                    }
                    break;
                case "sentence": {
                    String s = msg.optString("text");
                    int seq = msg.optInt("seq", 0);
                    if (seq > 0) {
                        // 先记着，等这句的配音真的开始播了再上屏 —— 合成比朗读慢的时候
                        // 文字会跑到声音前面好几秒，聊天记录看着就像"没同步"
                        synchronized (pendingTexts) {
                            pendingTexts.put(seq, s);
                        }
                    } else {
                        status("🤖 " + s);       // 旧版服务端没有 seq
                        dialogue("小音", s);
                    }
                    break;
                }
                case "audio":
                    // 紧接着的那个二进制帧就是这一段的 mp3
                    pendingAudioSeq = msg.optInt("seq", 0);
                    break;
                case "status":
                    status(msg.optString("text"));
                    break;
                case "barge_in":
                    stopPlayback();     // 只做本地停播，不用回消息（服务端已进入新一轮聆听）
                    status("⏹ 已打断 — 请继续说");
                    state("idle");
                    break;
                case "speaker_reject":
                    status("🔇 声音不是主人，已忽略");
                    state("idle");
                    break;
                case "turn_end":
                    flushPendingTexts();        // 没配上音的那几句也别丢
                    status(msg.optString("status", "✅ 完成"));
                    state("idle");
                    break;
                case "error":
                    status("⚠️ " + msg.optString("error"));
                    state("error");
                    break;
                default:
                    break;
            }
        } catch (Exception ignored) {
            // 协议外的消息忽略
        }
    }

    private void status(String text) {
        if (listener != null) {
            listener.onStatus(text);
        }
    }

    private void dialogue(String who, String text) {
        if (listener != null) {
            listener.onDialogue(who, text);
        }
    }

    private void state(String s) {
        if (listener != null) {
            listener.onState(s);
        }
    }

    // ── 采集（全程不暂停：播报期间也要推流，否则打断检测失效）────
    //
    // 采集逻辑已经搬到 MicCapture（两条链路共用同一份：连电脑的推 WS，手机独立的喂本地引擎）。
    // 这里只负责"拿到帧就发出去"。

    private void startMic() {
        mic.start();
    }

    private void stopMic() {
        mic.stop();
    }

    // ── 播放（一句 mp3 一个临时文件，顺序播）────────────────

    /** 一段配音：mp3 字节 + 它对应的那句文字（seq）。 */
    private static final class Clip {
        final byte[] data;
        final int seq;

        Clip(byte[] data, int seq) {
            this.data = data;
            this.seq = seq;
        }
    }

    private void enqueueMp3(byte[] mp3, int seq) {
        synchronized (playQueue) {
            playQueue.add(new Clip(mp3, seq));
        }
        handler.post(this::playNext);
    }

    private void playNext() {
        synchronized (playQueue) {
            if (playing || playQueue.isEmpty()) {
                return;
            }
            playing = true;
        }
        Clip clip;
        synchronized (playQueue) {
            clip = playQueue.poll();
        }
        if (clip == null) {
            playing = false;
            return;
        }
        try {
            currentFile = File.createTempFile("xiaoyin", ".mp3", context.getCacheDir());
            try (FileOutputStream fos = new FileOutputStream(currentFile)) {
                fos.write(clip.data);
            }
            MediaPlayer mp = new MediaPlayer();
            player = mp;
            mp.setDataSource(currentFile.getAbsolutePath());
            mp.setOnCompletionListener(m -> finishOne());
            mp.setOnErrorListener((m, what, extra) -> {
                finishOne();
                return true;
            });
            mp.prepareAsync();
            mp.setOnPreparedListener(m -> {
                onClipStart(clip);      // 声音真的响了，这句才上屏
                m.start();
            });
        } catch (Exception e) {
            finishOne();
        }
    }

    /** 某段配音开始播放：把对应文字写进对话记录，并更新状态行。 */
    private void onClipStart(Clip clip) {
        String text;
        synchronized (pendingTexts) {
            text = pendingTexts.remove(clip.seq);
        }
        if (text != null) {
            status("🔊 小音：" + text);
            dialogue("小音", text);
        }
        state("speak");
    }

    /** 收了尾还没配上音的句子（比如那句合成失败）：按顺序补记，不丢内容。 */
    private void flushPendingTexts() {
        java.util.List<Integer> keys;
        synchronized (pendingTexts) {
            keys = new java.util.ArrayList<>(pendingTexts.keySet());
            java.util.Collections.sort(keys);
        }
        for (Integer k : keys) {
            String text;
            synchronized (pendingTexts) {
                text = pendingTexts.remove(k);
            }
            if (text != null) {
                dialogue("小音", text);
            }
        }
    }

    private void finishOne() {
        try {
            if (player != null) {
                player.release();
            }
        } catch (Exception ignored) {
        }
        player = null;
        if (currentFile != null) {
            //noinspection ResultOfMethodCallIgnored
            currentFile.delete();
            currentFile = null;
        }
        playing = false;
        handler.post(this::playNext);       // 接着播下一句
    }

    private void stopPlayback() {
        synchronized (playQueue) {
            playQueue.clear();
        }
        synchronized (pendingTexts) {
            pendingTexts.clear();       // 打断后这些句子不会再播了，别再等着上屏
        }
        pendingAudioSeq = 0;
        playing = false;
        state("idle");
        try {
            if (player != null) {
                player.stop();
                player.release();
            }
        } catch (Exception ignored) {
        }
        player = null;
        if (currentFile != null) {
            //noinspection ResultOfMethodCallIgnored
            currentFile.delete();
            currentFile = null;
        }
    }
}
