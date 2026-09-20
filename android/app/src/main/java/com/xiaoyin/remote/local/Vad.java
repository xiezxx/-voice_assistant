package com.xiaoyin.remote.local;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * 端侧的语音活动判定：什么时候算"开始说话"、什么时候算"说完了"、什么时候算"在打断"。
 *
 * <p>{@link UtteranceCollector} 和 {@link BargeDetector} 是<b>逐行照搬</b>电脑端
 * {@code voice_server.py} 的实现（那套参数是调出来的：0.8s 尾静音、连续 3 块判打断）。
 *
 * <p>但<b>门限的算法故意不一样</b>，原因很实在：电脑端 {@code _calibrate_vad} 是
 * {@code max(基础/4, min(基础, 唤醒峰值/2))}，天花板被钉死在 0.02 这个<b>绝对量</b>上。
 * 电脑的麦克风是独立硬件、增益固定，所以没事；手机麦克风普遍开 AGC（自动增益），
 * 安静房间的底噪就可能超过 0.02 —— 那样这个 VAD 会<b>永远认为有人在说话</b>：
 * 刚唤醒就立刻判定"说完了"，或者播报一开始就自己打断自己。
 *
 * <p>所以端侧改成<b>跟着本底噪声走</b>，并且把天花板放开到 0.25：底噪多少，门限就在它上面一点。
 */
public final class Vad {

    // ── 门限相关的常量（手机上调过之后再来改这几个）──
    /** 绝对下限：再安静的房间也不至于比这更灵敏，否则喘气都算说话 */
    public static final float ABS_FLOOR = 0.006f;
    /** 天花板：底噪再大也不能高过它，否则正常说话都触发不了 */
    public static final float MAX_THRESHOLD = 0.25f;
    /** 聆听门 = 噪声底 × 这个倍数 */
    public static final float NOISE_MULT = 3.0f;
    /** 唤醒时按人声峰值定门：峰值 × 这个比例（往低调，照顾说话轻的人） */
    public static final float WAKE_RATIO = 0.35f;
    /** 打断门 = 聆听门 × 这个倍数（打断是用户主动提高音量，本来就比正常说话响） */
    public static final float BARGE_MULT = 1.6f;
    /** 打断门的另一条下界：噪声底 × 这个倍数 */
    public static final float BARGE_NOISE_MULT = 5.0f;

    // ── 时序常量（照搬电脑端）──
    /** 电平环形缓冲长度：3 秒（每块 0.1s） */
    public static final int LEVEL_RING = 30;
    /** 取第 20 百分位当噪声底：说话时被高电平盖住，取值鲁棒 */
    public static final int NOISE_PERCENTILE = 20;
    /** 尾静音 0.8s 判定说完 */
    public static final int SILENCE_FRAMES = 8;
    /** 最短有效话语 0.3s */
    public static final int MIN_FRAMES = 3;
    /** 最长 12s，超了强制切走 */
    public static final int MAX_FRAMES = 120;
    /** 连续 3 块（0.3s）有能量判打断；静音即清零 */
    public static final int BARGE_STREAK = 3;

    private Vad() {
    }

    /** 16k 单声道 PCM16 一帧的"能量"：平均绝对值，对齐 Python 的 {@code np.abs(x).mean()}。 */
    public static float level(short[] frame, int len) {
        if (frame == null || len <= 0) {
            return 0f;
        }
        long sum = 0;
        for (int i = 0; i < len; i++) {
            int v = frame[i];
            sum += (v < 0 ? -v : v);
        }
        // 除以 32768 换算到 [-1,1]，和电脑端 float 样本的尺度一致，常量才能照搬
        return (float) (sum / (double) len / 32768.0);
    }

    /** 最近 N 帧的电平，用来估本底噪声和提供调试读数。 */
    public static final class LevelRing {
        private final float[] buf = new float[LEVEL_RING];
        private int size = 0;
        private int next = 0;

        public void push(float level) {
            buf[next] = level;
            next = (next + 1) % LEVEL_RING;
            if (size < LEVEL_RING) {
                size++;
            }
        }

        public int size() {
            return size;
        }

        public boolean isEmpty() {
            return size == 0;
        }

        /** 最近一段里的最大电平（唤醒时用它定门）。 */
        public float max() {
            float m = 0f;
            for (int i = 0; i < size; i++) {
                m = Math.max(m, buf[i]);
            }
            return m;
        }

        /** 第 p 百分位（线性插值，和 numpy.percentile 的默认算法一致）。 */
        public float percentile(int p) {
            if (size == 0) {
                return 0f;
            }
            float[] sorted = Arrays.copyOf(buf, size);
            Arrays.sort(sorted);
            float rank = (size - 1) * p / 100f;
            int lo = (int) Math.floor(rank);
            int hi = (int) Math.ceil(rank);
            if (lo == hi) {
                return sorted[lo];
            }
            float frac = rank - lo;
            return sorted[lo] * (1 - frac) + sorted[hi] * frac;
        }

        /** 本底噪声估计。 */
        public float noiseFloor() {
            return percentile(NOISE_PERCENTILE);
        }
    }

    /** 由噪声底推出聆听门；再被唤醒峰值往下压一点（照顾说话轻的人）。 */
    public static float speechThreshold(float noiseFloor, float wakePeak, boolean atWake,
                                        float sensitivity) {
        float t = clamp(Math.max(noiseFloor * NOISE_MULT, ABS_FLOOR), ABS_FLOOR, MAX_THRESHOLD);
        if (atWake && wakePeak > 0f) {
            // 只许往低调，且不允许低于底噪推出来的门（电脑端这里缺下界保护）
            t = clamp(Math.min(t, wakePeak * WAKE_RATIO), t, MAX_THRESHOLD);
        }
        // 灵敏度：设置里 ± 出来的倍数，越大越灵敏（门限越低）
        if (sensitivity > 0f) {
            t = t / sensitivity;
        }
        return clamp(t, 0.0005f, MAX_THRESHOLD);
    }

    /** 打断门比聆听门高：用户主动打断本来就比正常说话响，且回声残余通常就在底噪附近。 */
    public static float bargeThreshold(float speechThreshold, float noiseFloor) {
        return Math.max(speechThreshold * BARGE_MULT, noiseFloor * BARGE_NOISE_MULT);
    }

    private static float clamp(float v, float lo, float hi) {
        return v < lo ? lo : (v > hi ? hi : v);
    }

    /**
     * 采集一句话：喂 0.1s 块，尾静音够了就算说完。逐行照搬电脑端的 {@code UtteranceCollector}。
     *
     * <p>注意它连"说话前"的 {@value #SILENCE_FRAMES} 块预卷都留着 —— 因为唤醒词之后
     * 用户可能立刻开口，预卷能保证字头不被切掉。
     */
    public static final class UtteranceCollector {
        private final float threshold;
        private final List<short[]> frames = new ArrayList<>();
        private boolean hasSpeech = false;
        private int silence = 0;
        private boolean done = false;

        public UtteranceCollector(float threshold) {
            this.threshold = threshold;
        }

        /** 喂一块（建议 1600 样本），返回这块之后是否采集完成。 */
        public boolean feed(short[] frame, int len, float level) {
            if (done) {
                return true;
            }
            short[] copy = Arrays.copyOf(frame, len);
            if (!hasSpeech) {
                if (level >= threshold) {
                    hasSpeech = true;
                    frames.add(copy);
                } else {
                    // 前置静音：只保留一段预卷，丢弃最老的
                    frames.add(copy);
                    if (frames.size() > SILENCE_FRAMES) {
                        frames.remove(0);
                    }
                }
                return false;
            }
            frames.add(copy);
            if (level < threshold) {
                silence++;
                if (silence >= SILENCE_FRAMES) {
                    done = true;
                }
            } else {
                silence = 0;
            }
            if (frames.size() >= MAX_FRAMES) {
                done = true;
            }
            return done;
        }

        /** 取出采集到的音频（float32，直接喂识别器）。 */
        public float[] take() {
            int total = 0;
            for (short[] f : frames) {
                total += f.length;
            }
            float[] out = new float[total];
            int at = 0;
            for (short[] f : frames) {
                for (short s : f) {
                    out[at++] = s / 32768.0f;
                }
            }
            return out;
        }

        public int frameCount() {
            return frames.size();
        }

        public boolean hasSpeech() {
            return hasSpeech;
        }

        public boolean isDone() {
            return done;
        }
    }

    /** 打断检测：连续 {@value #BARGE_STREAK} 块超阈值即判定，静音清零。照搬电脑端。 */
    public static final class BargeDetector {
        private final float threshold;
        private int streak = 0;

        public BargeDetector(float threshold) {
            this.threshold = threshold;
        }

        public boolean feed(float level) {
            if (level > threshold) {
                streak++;
                return streak >= BARGE_STREAK;
            }
            streak = 0;
            return false;
        }

        public void reset() {
            streak = 0;
        }
    }
}
