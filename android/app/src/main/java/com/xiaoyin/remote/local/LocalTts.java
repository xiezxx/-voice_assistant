package com.xiaoyin.remote.local;

import android.content.Context;
import android.os.Bundle;
import android.speech.tts.TextToSpeech;
import android.speech.tts.UtteranceProgressListener;
import android.speech.tts.Voice;

import java.util.Locale;
import java.util.Set;

/**
 * 语音合成（端侧）：让手机把回复念出来。这一版用**系统自带的 TTS**（0MB）。
 *
 * <p>选它的理由：国产 ROM 基本都自带离线中文引擎，不用下载、不用打包模型。
 * 代价是音色随手机而定，而且如果只装了 Google TTS 又没下中文离线包，就用不了 ——
 * 所以 {@link #init} 会做两件事：报告引擎名（给调试面板看）+ 检查"是否需要联网"
 * （需要联网 = 没有离线中文包，端侧模式的意义就没了）。
 *
 * <p>两个坑（都是 Android 的，不是我们的）：
 * <ol>
 *   <li>{@code TextToSpeech} 必须在**有 Looper 的线程**构造，否则 onInit 收不到；
 *   <li>清单里必须有 {@code <queries><intent><action TTS_SERVICE>}，否则 onInit 直接 ERROR。
 * </ol>
 */
public class LocalTts {

    public interface Listener {
        /** 引擎就绪（ok=false 表示用不了，reason 给用户看） */
        void onReady(boolean ok, String engineLabel, String reason);

        /** 某一句开始出声：这时候才算"在播报"（界面状态球要变蓝、打断检测要开） */
        void onStart(String utteranceId);

        /** 某一句念完了（正常或出错都算） */
        void onDone(String utteranceId);
    }

    private final Context context;
    private final Listener listener;
    private TextToSpeech tts;
    private volatile boolean ready;

    public LocalTts(Context context, Listener listener) {
        this.context = context.getApplicationContext();
        this.listener = listener;
    }

    /** 构造 TTS 引擎。必须在主线程调（见类注释第 1 条）。 */
    public void init() {
        tts = new TextToSpeech(context, status -> {
            if (status != TextToSpeech.SUCCESS) {
                ready = false;
                listener.onReady(false, "无", "系统语音合成初始化失败");
                return;
            }
            String label = describeEngine();
            int langResult = tts.setLanguage(Locale.CHINA);
            if (langResult == TextToSpeech.LANG_MISSING_DATA
                    || langResult == TextToSpeech.LANG_NOT_SUPPORTED) {
                ready = false;
                listener.onReady(false, label, "系统里没有中文语音包，去「设置 → 语音合成」里装一个");
                return;
            }
            tts.setSpeechRate(1.05f);      // 略快一点，语音助手听着更自然
            tts.setOnUtteranceProgressListener(new UtteranceProgressListener() {
                @Override
                public void onStart(String utteranceId) {
                    listener.onStart(utteranceId);
                }

                @Override
                public void onDone(String utteranceId) {
                    listener.onDone(utteranceId);
                }

                @Override
                public void onError(String utteranceId) {
                    listener.onDone(utteranceId);   // 出错也要销账，否则状态会卡在"播报中"
                }

                @Override
                public void onError(String utteranceId, int errorCode) {
                    listener.onDone(utteranceId);
                }
            });
            ready = true;
            listener.onReady(true, label, "");
        });
    }

    public boolean isReady() {
        return ready;
    }

    /** 引擎信息（调试面板要显示）：引擎名 + 当前音色 + 是否要联网。 */
    private String describeEngine() {
        String engine = "系统 TTS";
        try {
            if (tts.getDefaultEngine() != null) {
                engine = tts.getDefaultEngine();
            }
        } catch (Exception ignored) {
        }
        Voice v = null;
        try {
            v = tts.getVoice();
        } catch (Exception ignored) {
        }
        StringBuilder sb = new StringBuilder(engine);
        if (v != null) {
            sb.append(" / ").append(v.getName());
            if (v.isNetworkConnectionRequired()) {
                sb.append("（⚠️ 需要联网，建议装离线中文包）");
            } else {
                sb.append("（离线）");
            }
        }
        // 有中文离线音色吗？没有的话端侧播报会用不了
        try {
            Set<Voice> voices = tts.getVoices();
            boolean zhOffline = false;
            if (voices != null) {
                for (Voice one : voices) {
                    if ("zh".equals(one.getLocale().getLanguage()) && !one.isNetworkConnectionRequired()) {
                        zhOffline = true;
                        break;
                    }
                }
            }
            if (!zhOffline) {
                sb.append(" [无离线中文音色]");
            }
        } catch (Exception ignored) {
        }
        return sb.toString();
    }

    /**
     * 念一句。{@code utteranceId} 由调用方给（用序号），用来把 onStart/onDone 对上号 ——
     * 界面靠 onStart 才把文字上屏，所以这个 id 必须唯一。
     */
    public void speak(String text, String utteranceId) {
        if (!ready || tts == null || text == null || text.isEmpty()) {
            listener.onDone(utteranceId);      // 没念出来也要销账
            return;
        }
        Bundle params = new Bundle();
        int r = tts.speak(text, TextToSpeech.QUEUE_ADD, params, utteranceId);
        if (r != TextToSpeech.SUCCESS) {
            listener.onDone(utteranceId);
        }
    }

    /** 立刻停止（打断用）。已排队没念的不会再念，但它们的 onDone 不会回调 —— 调用方要自己清账。 */
    public void stop() {
        if (tts != null) {
            tts.stop();
        }
    }

    public void shutdown() {
        ready = false;
        if (tts != null) {
            tts.stop();
            tts.shutdown();
            tts = null;
        }
    }
}
