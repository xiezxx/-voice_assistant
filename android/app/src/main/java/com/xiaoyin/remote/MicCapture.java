package com.xiaoyin.remote;

import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.media.audiofx.AcousticEchoCanceler;
import android.media.audiofx.AudioEffect;
import android.media.audiofx.NoiseSuppressor;

import java.util.ArrayList;
import java.util.List;

/**
 * 麦克风采集：从 44.1k/48k 的原始采样里攒出**恰好 0.1 秒（1600 样本）**的 16k 帧。
 *
 * <p>这段代码是从 {@link VoiceClient} 里原样搬出来的，两条链路共用：
 * 连电脑那条（{@code /ws/assistant} 推流）和手机独立那条（喂本地唤醒/识别）。
 * <b>搬家时一行逻辑都没改</b>，因为下面四个坑都是踩出来的：
 *
 * <ol>
 *   <li><b>音源要按顺序试</b>：{@code VOICE_RECOGNITION} → {@code MIC} →
 *       {@code VOICE_COMMUNICATION}。某些机型上 {@code VOICE_COMMUNICATION} 没有通话时
 *       会返回<b>一片静音</b>，那样唤醒词永远检测不到 —— 所以它只当最后的退路。</li>
 *   <li><b>回声消除必须留引用</b>：{@code AcousticEchoCanceler}/{@code NoiseSuppressor}
 *       创建后不留引用会被 GC 回收，效果随之失效（还不报错）。</li>
 *   <li><b>攒够整帧再交出去</b>：{@code read()} 常常只返回一半，读多少交多少的话
 *       每帧代表的时长就短了，服务端按"帧数 × 0.1 秒"算时间轴会整体跑快 ——
 *       唤醒词和识别都会失败。</li>
 *   <li><b>重采样要用窗口平均</b>：直接抽点会让高频折返成噪声（降采样必须带低通），
 *       而端侧那个识别模型对这点比 Whisper 敏感。</li>
 * </ol>
 */
public class MicCapture {

    /** 一帧的样本数：0.1 秒 @16k */
    public static final int FRAME_SAMPLES = 1600;
    public static final int SAMPLE_RATE = 16000;

    /** 依次尝试的音源：识别优化 → 裸麦克风 → 通话（最后才用，见类注释第 1 条）。 */
    private static final int[] SOURCES = {
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            MediaRecorder.AudioSource.MIC,
            MediaRecorder.AudioSource.VOICE_COMMUNICATION,
    };

    public interface Listener {
        /** 一帧好了：恰好 {@link #FRAME_SAMPLES} 个样本（小端无关，交给调用方处理）。 */
        void onFrame(short[] frame);

        /** 状态文案（音源降级、打不开麦克风等），由调用方决定给谁看。 */
        void onStatus(String text);
    }

    private final Listener listener;
    private final List<AudioEffect> effects = new ArrayList<>();

    private AudioRecord record;
    private Thread micThread;
    private volatile boolean running;
    private int nativeChunk = FRAME_SAMPLES;      // 按实测采样率折算的要读多少样本
    private int sourceUsed = -1;                  // 实际用上的音源（调试面板要显示）
    private boolean aecOn, nsOn;                  // 回声消除/降噪是否真的启用了

    public MicCapture(Listener listener) {
        this.listener = listener;
    }

    public void start() {
        if (micThread != null) {
            return;
        }
        running = true;
        micThread = new Thread(this::micLoop, "xiaoyin-mic");
        micThread.start();
    }

    public void stop() {
        running = false;
        Thread t = micThread;
        micThread = null;
        if (t != null) {
            t.interrupt();
        }
        for (AudioEffect e : effects) {
            try {
                e.release();
            } catch (Exception ignored) {
            }
        }
        effects.clear();
        if (record != null) {
            try {
                record.stop();
            } catch (Exception ignored) {
            }
            try {
                record.release();
            } catch (Exception ignored) {
            }
            record = null;
        }
    }

    public int sourceUsed() {
        return sourceUsed;
    }

    public int nativeSampleRate() {
        return record != null ? record.getSampleRate() : 0;
    }

    public boolean isAecOn() {
        return aecOn;
    }

    public boolean isNsOn() {
        return nsOn;
    }

    // ── 内部 ────────────────────────────────────────────────

    private void micLoop() {
        int minBuf = AudioRecord.getMinBufferSize(
                SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        if (!openMic(Math.max(minBuf, FRAME_SAMPLES * 2 * 4))) {
            status("⚠️ 麦克风打不开（检查是否授予了麦克风权限）");
            return;
        }
        // "请求"16k 不一定被满足：按实测采样率折算一帧要攒多少样本，再重采样回 16k
        int actual = record.getSampleRate();
        nativeChunk = Math.round(FRAME_SAMPLES * (actual > 0 ? actual : SAMPLE_RATE) / (float) SAMPLE_RATE);

        // 外放的声音会被自己的麦克风收进去 → 助手播报时把自己打断。有回声消除就用上
        aecOn = enableEffect(AcousticEchoCanceler.create(record.getAudioSessionId()));
        nsOn = enableEffect(NoiseSuppressor.create(record.getAudioSessionId()));

        try {
            record.startRecording();
        } catch (Exception e) {
            status("⚠️ 无法开始录音：" + e.getMessage());
            return;
        }
        status("🎙 聆听中 — 说「小音」唤醒");

        // 攒够整整一帧才交（见类注释第 3 条）
        short[] acc = new short[nativeChunk];
        int filled = 0;
        while (running && micThread == Thread.currentThread()) {
            int n;
            try {
                n = record.read(acc, filled, acc.length - filled);
            } catch (Exception e) {
                break;
            }
            if (n <= 0) {
                continue;
            }
            filled += n;
            if (filled < acc.length) {
                continue;
            }
            short[] frame = resample(acc, acc.length);
            if (frame != null && listener != null) {
                listener.onFrame(frame);
            }
            filled = 0;
        }
        stop();
    }

    /** 按优先级打开麦克风，成功返回 true。 */
    private boolean openMic(int bufSize) {
        for (int src : SOURCES) {
            AudioRecord r = null;
            try {
                r = new AudioRecord(src, SAMPLE_RATE, AudioFormat.CHANNEL_IN_MONO,
                        AudioFormat.ENCODING_PCM_16BIT, bufSize);
                if (r.getState() == AudioRecord.STATE_INITIALIZED) {
                    record = r;
                    sourceUsed = src;
                    if (src != SOURCES[0]) {
                        status("🎙 已改用备用音源（" + src + "）— 说「小音」唤醒");
                    }
                    return true;
                }
            } catch (Exception ignored) {
                // 这个音源这台机器不支持，试下一个
            }
            if (r != null) {
                try {
                    r.release();
                } catch (Exception ignored) {
                }
            }
        }
        return false;
    }

    /** 启用音效；成功返回 true。注意必须把引用存下来，否则会被 GC 回收（见类注释第 2 条）。 */
    private boolean enableEffect(AudioEffect effect) {
        if (effect == null) {
            return false;
        }
        try {
            effect.setEnabled(true);
            effects.add(effect);
            return true;
        } catch (Exception e) {
            try {
                effect.release();
            } catch (Exception ignored) {
            }
            return false;
        }
    }

    /** 把读到的样本重采样成恰好 {@link #FRAME_SAMPLES} 个（窗口平均当低通，见类注释第 4 条）。 */
    static short[] resample(short[] src, int n) {
        short[] out = new short[FRAME_SAMPLES];
        if (n == FRAME_SAMPLES) {
            System.arraycopy(src, 0, out, 0, n);
            return out;
        }
        if (n <= 0) {
            return null;
        }
        double step = (double) n / FRAME_SAMPLES;
        for (int i = 0; i < FRAME_SAMPLES; i++) {
            int start = (int) Math.floor(i * step);
            int end = Math.min((int) Math.ceil((i + 1) * step), n);
            if (end <= start) {
                end = Math.min(start + 1, n);
            }
            long sum = 0;
            for (int j = start; j < end; j++) {
                sum += src[j];
            }
            out[i] = (short) (sum / Math.max(1, end - start));
        }
        return out;
    }

    /** 小端 int16 打包（连电脑那条要发二进制帧）。 */
    public static byte[] toLittleEndian(short[] samples) {
        byte[] bytes = new byte[samples.length * 2];
        for (int i = 0; i < samples.length; i++) {
            bytes[i * 2] = (byte) (samples[i] & 0xFF);
            bytes[i * 2 + 1] = (byte) ((samples[i] >> 8) & 0xFF);
        }
        return bytes;
    }

    private void status(String text) {
        if (listener != null) {
            listener.onStatus(text);
        }
    }
}
