package com.xiaoyin.remote.local;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.Looper;

import com.xiaoyin.remote.MicCapture;
import com.xiaoyin.remote.VoiceClient;
import com.xiaoyin.remote.VoiceEngine;

import java.util.Locale;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;

/**
 * 手机独立模式的语音链路：**唤醒、识别、合成都跑在手机上**，不需要电脑。
 *
 * <p>状态机照搬电脑端 {@code voice_server.py} 的三态（IDLE / LISTENING / REPLYING），
 * 判据也照搬（0.8s 尾静音、连续 3 块判打断……），但**门限算法故意不同** —— 见 {@link Vad}。
 *
 * <p>三条线程，分工和电脑端的 actor 模型一致：
 * <ul>
 *   <li><b>采集线程</b>（{@link MicCapture} 内部）：每帧算电平、按状态分发。
 *       <b>所有 DSP/KWS/VAD/识别都在这里</b>，不做任何网络 IO —— 保证判断永不被阻塞，
 *       这是"采集永不暂停"在端侧的对应物；</li>
 *   <li><b>轮次线程</b>：取识别结果 → 交给大模型（P5）→ 逐句播报；</li>
 *   <li><b>主线程</b>：TextToSpeech 必须在有 Looper 的线程构造，所以它在主线程建。</li>
 * </ul>
 *
 * <p>本阶段（P3）先把识别结果**原样念回去**，用来独立验证端侧链路；
 * 接大模型是下一步的事（{@link #handleTurn} 里那一行换成 LlmClient 即可）。
 */
public class LocalVoiceEngine implements VoiceEngine {

    private static final int SAMPLE_RATE = 16000;
    /** 唤醒后等开口的上限（对齐服务端 LISTEN_NO_SPEECH_TIMEOUT） */
    private static final long LISTEN_TIMEOUT_MS = 15000;
    /** 起播静默窗：刚出声那一下自己的外放最响，先别听打断（对齐服务端的 BARGE_GRACE_SEC） */
    private static final long BARGE_GRACE_MS = 400;

    private static final String PREFS = "xiaoyin";
    private static final String KEY_SENSITIVITY = "sensitivity";

    public enum State { IDLE, LISTENING, REPLYING }

    /** DeepSeek 的 Key 存在这个偏好里（设置界面填，不预置进 APK —— 包里的字符串随便就能解出来） */
    public static final String KEY_API = "deepseek_key";
    /** 用哪个大模型（设置界面填，改完下一句就生效） */
    public static final String KEY_LLM_MODEL = "llm_model";

    private final Context context;
    private final VoiceClient.Listener ui;

    private MicCapture mic;
    private KwsSpotter kws;
    private AsrEngine asr;
    private LocalTts tts;
    private LlmClient llm;

    private final Handler main = new Handler(Looper.getMainLooper());
    private Thread turnThread;
    private final BlockingQueue<String> turnQueue = new ArrayBlockingQueue<>(2);

    private volatile boolean running;
    /** 三态机。只在采集线程写，其它线程只读（volatile 够用，不引锁） */
    private volatile State state = State.IDLE;

    private final Vad.LevelRing levelRing = new Vad.LevelRing();
    private Vad.UtteranceCollector collector;
    private Vad.BargeDetector barge;

    private volatile float speechThreshold = Vad.ABS_FLOOR * 2;
    private volatile float bargeThreshold = Vad.ABS_FLOOR * 2;
    private volatile float lastLevel = 0f;
    private volatile long listenDeadline = 0;

    // ── 播报状态（回声防护用，见 Vad 的注释）──
    private volatile boolean ttsBusy = false;
    private volatile long echoTailUntil = 0;
    private volatile long lastSpeakStart = 0;
    private volatile int pendingUtterances = 0;
    private volatile int bargeIgnoredCount = 0;
    private volatile boolean bargeMutedByEcho = false;   // 自愈：回声太多就临时关掉播报打断

    // ── 调试读数（面板要显示，回答"到底是哪里不对"）──
    private volatile int kwsHits = 0;
    private volatile String lastTranscript = "";
    private volatile float lastPeak = 0f;
    private volatile String ttsInfo = "未初始化";
    private volatile String loadInfo = "";

    public LocalVoiceEngine(Context context, VoiceClient.Listener ui) {
        this.context = context.getApplicationContext();
        this.ui = ui;
    }

    // ── 生命周期 ────────────────────────────────────────────

    @Override
    public void start() {
        if (running) {
            return;
        }
        running = true;
        status("🧠 端侧模式启动中…");
        state("think");

        LocalTools tools = new LocalTools(context);
        llm = new LlmClient(apiKey(), tools);

        mic = new MicCapture(new MicCapture.Listener() {
            @Override
            public void onFrame(short[] frame) {
                onMicFrame(frame);
            }

            @Override
            public void onStatus(String text) {
                status(text);
            }
        });

        // 模型加载要几秒（识别模型 63MB），别卡住服务启动
        new Thread(this::loadModelsThenStart, "xiaoyin-load").start();
        // 轮次线程：取识别结果 → 播报
        turnThread = new Thread(this::turnLoop, "xiaoyin-turn");
        turnThread.start();
    }

    private void loadModelsThenStart() {
        try {
            kws = new KwsSpotter(context.getAssets(), 0.25f);
            loadInfo = "唤醒词就绪";
            main.post(() -> {
                if (!running) {
                    return;
                }
                tts = new LocalTts(context, ttsListener);
                tts.init();
            });
            asr = new AsrEngine(context.getAssets());
            loadInfo = "唤醒词 + 识别 就绪";
            if (!running) {
                return;
            }
            mic.start();          // 采集和唤醒从这一刻开始
        } catch (Exception e) {
            status("⚠️ 端侧模型加载失败：" + e.getMessage());
            state("error");
        }
    }

    @Override
    public void stop() {
        running = false;
        if (mic != null) {
            mic.stop();
            mic = null;
        }
        if (tts != null) {
            main.post(() -> {
                if (tts != null) {
                    tts.shutdown();
                }
            });
        }
        if (kws != null) {
            kws.release();
            kws = null;
        }
        if (asr != null) {
            asr.release();
            asr = null;
        }
        if (turnThread != null) {
            turnThread.interrupt();
            turnThread = null;
        }
        state("off");
        status("语音助手已关闭");
    }

    @Override
    public void sendControl(String json) {
        // 端侧暂时没有需要接收的控制消息（连续对话等以后再说）
    }

    // ── 采集线程：全部信号处理都在这里 ──────────────────────

    private void onMicFrame(short[] frame) {
        if (!running) {
            return;
        }
        float level = Vad.level(frame, frame.length);
        lastLevel = level;
        levelRing.push(level);      // 任何状态都更新：噪声底估计和调试读数都要它

        switch (state) {
            case IDLE:
                // 播报门：自己正在说话（或刚说完的余音）时，不喂唤醒词 ——
                // 否则小音念到「小音」两个字会把自己唤醒
                if (ttsBusy || System.currentTimeMillis() < echoTailUntil || kws == null) {
                    return;
                }
                String kw = kws.feed(frame);
                if (kw != null && !kw.isEmpty()) {
                    enterListening();
                }
                break;

            case LISTENING:
                if (collector == null) {
                    return;
                }
                boolean done = collector.feed(frame, frame.length, level);
                if (done) {
                    finishListening();
                } else if (System.currentTimeMillis() > listenDeadline) {
                    collector = null;
                    state = State.IDLE;
                    status("👂 没有听到声音，先歇着，说「小音」再叫我");
                    state("idle");
                    kwsReset();
                }
                break;

            case REPLYING:
                if (barge == null || !bargeArmed()) {
                    return;
                }
                if (barge.feed(level)) {
                    onBarge();
                }
                break;
        }
        updateDebug();
    }

    private void enterListening() {
        kwsHits++;
        speechThreshold = Vad.speechThreshold(levelRing.noiseFloor(), levelRing.max(), true, sensitivity());
        bargeThreshold = Vad.bargeThreshold(speechThreshold, levelRing.noiseFloor());
        collector = new Vad.UtteranceCollector(speechThreshold);
        listenDeadline = System.currentTimeMillis() + LISTEN_TIMEOUT_MS;
        state = State.LISTENING;
        status("🔔 唤醒成功 — 请说话");
        state("wake");
        updateDebug();
    }

    private void finishListening() {
        Vad.UtteranceCollector col = collector;
        collector = null;
        float[] audio = col == null ? new float[0] : col.take();
        if (audio.length < SAMPLE_RATE * 0.3f) {
            state = State.IDLE;
            status("⚠️ 语音太短没听清，说「小音」再试一次");
            state("idle");
            kwsReset();
            return;
        }
        // 峰值：判断麦克风是不是太小声/削顶（调试面板要）
        float peak = 0f;
        for (float v : audio) {
            peak = Math.max(peak, Math.abs(v));
        }
        lastPeak = peak;

        status("🎤 正在识别…");
        state("think");
        String text;
        try {
            text = asr == null ? "" : asr.transcribe(audio);
        } catch (Exception e) {
            text = "";
        }
        if (!running) {
            return;
        }
        if (text.isEmpty()) {
            state = State.IDLE;
            status("⚠️ 没识别到内容，说「小音」再试一次");
            state("idle");
            kwsReset();
            return;
        }
        lastTranscript = text;
        ui.onDialogue("我", text);
        status("🤖 「" + text + "」");
        state("think");
        state = State.REPLYING;
        if (!turnQueue.offer(text)) {
            turnQueue.poll();          // 队列满了就丢最老的，宁可漏一轮也别卡住
            turnQueue.offer(text);
        }
        updateDebug();
    }

    private void onBarge() {
        // 起播起步窗：人从听到声音到开口最快也要 300ms，窗口内的"语音"几乎必然是回声
        long sinceSpeak = System.currentTimeMillis() - lastSpeakStart;
        if (sinceSpeak < 300) {
            bargeIgnoredCount++;
            if (bargeIgnoredCount >= 3 && !bargeMutedByEcho) {
                bargeMutedByEcho = true;    // 自愈：这台机器回声太重，播报期间干脆不听打断
                status("🔇 检测到回声，已临时关闭播报打断（下次开机恢复）");
            }
            return;
        }
        if (tts != null) {
            tts.stop();
        }
        // 同时掐断大模型请求：轮次线程正阻塞在流式读上，cancel 会让它抛 IOException 退出
        if (llm != null) {
            llm.cancel();
        }
        pendingUtterances = 0;
        ttsBusy = false;
        state = State.LISTENING;
        collector = new Vad.UtteranceCollector(speechThreshold);
        listenDeadline = System.currentTimeMillis() + LISTEN_TIMEOUT_MS;
        status("⏹ 已打断 — 请继续说");
        state("idle");
        updateDebug();
    }

    /** 打断是否armed：播报门 + 起步窗 + 自愈开关 */
    private boolean bargeArmed() {
        if (!ttsBusy && System.currentTimeMillis() >= echoTailUntil) {
            return false;       // 没在播报就没必要检测打断
        }
        if (bargeMutedByEcho) {
            return false;
        }
        return true;
    }

    private void kwsReset() {
        if (kws != null) {
            kws.reset();
        }
    }

    // ── 轮次线程 ────────────────────────────────────────────

    private void turnLoop() {
        while (running) {
            String text;
            try {
                text = turnQueue.take();
            } catch (InterruptedException e) {
                return;
            }
            if (!running) {
                return;
            }
            handleTurn(text);
        }
    }

    /**
     * 一轮回复：大模型流式生成 → 边切句边播报（和电脑端同一条流水线）。
     *
     * <p>播报期间用户插话会 {@link LlmClient#cancel()} 掐断请求，这里把"被掐断"和
     * "真出错"分开处理 —— 打断是正常操作，不该弹错误。
     */
    private void handleTurn(String text) {
        // 每轮都重新读一遍设置：改了模型/Key **下一句就生效**，不需要重开语音助手
        String key = apiKey();
        if (llm == null || !key.equals(llmKey)) {
            llm = new LlmClient(key, new LocalTools(context));
            llmKey = key;
        }
        llm.setModel(modelName());

        if (llm == null || !llm.hasKey()) {
            speak("还没填 DeepSeek 的 Key，去右上角设置里填一下，我就能聊天了");
        } else {
            final SpeechSegmenter segmenter = new SpeechSegmenter();
            try {
                llm.stream(text, segmenter, new LlmClient.Sink() {
                    @Override
                    public void onSentence(String sentence) {
                        speak(sentence);
                    }

                    @Override
                    public void onStatus(String s) {
                        if (s != null && !s.isEmpty()) {
                            status(s);
                        }
                    }

                    @Override
                    public void onDone(String fullReply) {
                        if (fullReply != null && !fullReply.isEmpty()) {
                            lastReply = fullReply;
                        }
                    }
                });
            } catch (Exception e) {
                if (state == State.REPLYING && running) {
                    // 还在 REPLYING 说明不是被打断，是真出错
                    status("⚠️ 想不出来：" + e.getMessage());
                    speak("网络好像有点问题，等会儿再试试");
                }
                // 已经被打断（state 变成 LISTENING）就不用管，那是正常路径
            }
        }

        // 等播报排空再回待机。**必须有超时**：TTS 要是卡住不回调 onDone，
        // 没超时就会永远卡在 REPLYING，用户再喊也没反应
        long deadline = System.currentTimeMillis() + 60_000;
        while (running && pendingUtterances > 0 && System.currentTimeMillis() < deadline) {
            sleep(50);
        }
        if (pendingUtterances > 0) {
            pendingUtterances = 0;
            ttsBusy = false;
            status("⚠️ 播报超时，已跳过");
        }
        // 被打断的话 state 已经是 LISTENING 了，别把它按回 IDLE
        if (running && state == State.REPLYING) {
            state = State.IDLE;
            kwsReset();                 // 防止它自己念的话触发唤醒
            state("idle");
            updateDebug();
        }
    }

    /** 上一轮回复全文（调试面板要看） */
    private volatile String lastReply = "";
    /** 当前 LlmClient 是按哪个 Key 建的（Key 变了就重建） */
    private String llmKey = "";

    private String apiKey() {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_API, "");
    }

    /** 用户配的模型名（界面可改） */
    private String modelName() {
        String m = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
                .getString(KEY_LLM_MODEL, "");
        return m == null || m.trim().isEmpty() ? LlmClient.DEFAULT_MODEL : m.trim();
    }

    private void speak(String sentence) {
        if (tts == null || !tts.isReady()) {
            status("⚠️ 系统语音合成用不了：" + ttsInfo);
            return;                     // 不计数：免得"等播报排空"永远等不到
        }
        lastSpokenText = sentence;
        pendingUtterances++;            // 送进 TTS 队列的句数（念完才减）
        final String id = "u" + System.nanoTime();
        main.post(() -> {
            if (tts != null) {
                tts.speak(sentence, id);
            }
        });
    }

    private final LocalTts.Listener ttsListener = new LocalTts.Listener() {
        @Override
        public void onReady(boolean ok, String engineLabel, String reason) {
            ttsInfo = ok ? engineLabel : (engineLabel + " — " + reason);
            if (!ok) {
                status("⚠️ " + reason);
            }
            updateDebug();
        }

        @Override
        public void onStart(String utteranceId) {
            lastSpeakStart = System.currentTimeMillis();
            ttsBusy = true;
            // 声音真响了这句才上屏 —— 和电脑端"文字跟着配音走"是同一个做法
            ui.onDialogue("小音", lastSpokenText);
            status("🔊 小音：" + lastSpokenText);
            state("speak");
        }

        @Override
        public void onDone(String utteranceId) {
            if (pendingUtterances > 0) {
                pendingUtterances--;
            }
            if (pendingUtterances == 0) {
                ttsBusy = false;
                echoTailUntil = System.currentTimeMillis() + BARGE_GRACE_MS;
            }
            updateDebug();
        }
    };

    /** 正在念的这一句（onStart 要用它上屏） */
    private volatile String lastSpokenText = "";

    // ── 调试面板 ────────────────────────────────────────────

    private float sensitivity() {
        SharedPreferences p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        float v = p.getFloat(KEY_SENSITIVITY, 1.0f);
        return v <= 0f ? 1.0f : v;
    }

    private void updateDebug() {
        StringBuilder sb = new StringBuilder();
        if (mic != null) {
            sb.append("音源 ").append(mic.sourceUsed())
              .append(" 实测").append(mic.nativeSampleRate()).append("Hz")
              .append(" AEC").append(mic.isAecOn() ? "✅" : "❌")
              .append(" NS").append(mic.isNsOn() ? "✅" : "❌").append('\n');
        }
        sb.append(String.format(Locale.US,
                "噪声底 %.4f  聆听门 %.4f  打断门 %.4f  当前电平 %.4f%n",
                levelRing.noiseFloor(), speechThreshold, bargeThreshold, lastLevel));
        sb.append("状态 ").append(state)
          .append("  灵敏度 ×").append(String.format(Locale.US, "%.2f", sensitivity()))
          .append("  唤醒 ").append(kwsHits).append(" 次")
          .append(bargeMutedByEcho ? "  打断已关" : "").append('\n');
        sb.append("识别模型 ").append(loadInfo)
          .append("  合成 ").append(ttsInfo).append('\n');
        sb.append("大模型 ").append(modelName()).append('\n');
        if (!lastTranscript.isEmpty()) {
            sb.append("上次识别「").append(lastTranscript).append("」")
              .append("  峰值 ").append(String.format(Locale.US, "%.3f", lastPeak));
        }
        com.xiaoyin.remote.RemoteService.debugText = sb.toString();
    }

    // ── 小工具 ──────────────────────────────────────────────

    private void status(String text) {
        if (ui != null) {
            ui.onStatus(text);
        }
    }

    private void state(String s) {
        if (ui != null) {
            ui.onState(s);
        }
    }

    private void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
