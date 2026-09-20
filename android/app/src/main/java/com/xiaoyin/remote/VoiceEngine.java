package com.xiaoyin.remote;

/**
 * 一条语音链路。两个实现：
 *
 * <ul>
 *   <li>{@link VoiceClient} —— <b>连电脑</b>：采集推给电脑上的服务器，收 mp3 回来播；</li>
 *   <li>{@code LocalVoiceEngine}（手机独立模式）—— 唤醒/识别/合成都在手机上跑，只有大模型联网。</li>
 * </ul>
 *
 * <p>两者对外只暴露这三个方法，并且通过<b>同一个</b> {@link VoiceClient.Listener}
 * 回调（{@code onStatus/onDialogue/onState}）汇报状态 —— 所以界面（状态球六态、聊天气泡、
 * 300ms ticker）一行都不用改，切换模式也不用动 UI。
 */
public interface VoiceEngine {

    /** 开始工作（分配资源、起线程/连接）。重复调用应当无副作用。 */
    void start();

    /** 停止并释放资源。 */
    void stop();

    /** 发一条控制消息（连续对话开关等）。连电脑时是发给服务器的 JSON。 */
    void sendControl(String json);
}
