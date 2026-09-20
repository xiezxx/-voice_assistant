package com.xiaoyin.remote;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.Manifest;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.os.Build;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;

import androidx.core.app.NotificationCompat;

import org.json.JSONObject;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.concurrent.TimeUnit;

import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.WebSocket;
import okhttp3.WebSocketListener;

/**
 * 常驻服务：维持一条到小音服务器的 WebSocket（{@code /ws/device}），收到指令就执行。
 *
 * 服务器只在这两种情况下会推指令过来：
 * <ul>
 *   <li>你<b>用手机</b>对助手说「放首晴天」→ 服务器把歌推过来，这里调起 QQ 音乐播放</li>
 *   <li>你<b>用手机</b>说「暂停/下一首」→ 这里通过媒体会话控制 QQ 音乐</li>
 * </ul>
 * 用桌宠或电脑网页说话时，服务器会把歌放电脑上，**不会**推给手机。
 *
 * 两个必须由用户授予的权限（缺了对应功能就静默失效，App 里都有引导按钮）：
 * 「显示在其他应用上层」——否则 Android 10+ 不允许后台服务调起别的 App；
 * 「通知使用权」——否则拿不到 QQ 音乐的媒体会话来暂停/切歌。
 */
public class RemoteService extends Service {

    public static final String ACTION_START = "com.xiaoyin.remote.START";
    public static final String ACTION_STOP = "com.xiaoyin.remote.STOP";
    public static final String EXTRA_HOST = "host";
    /** 语音助手开关（一直听着的那套） */
    public static final String ACTION_VOICE = "com.xiaoyin.remote.VOICE";
    /** 连续对话开关 */
    public static final String ACTION_CONTINUOUS = "com.xiaoyin.remote.CONTINUOUS";
    /** 语音链路模式：连电脑 / 手机独立 */
    public static final String ACTION_MODE = "com.xiaoyin.remote.MODE";
    public static final String EXTRA_MODE = "mode";
    public static final String EXTRA_ENABLED = "enabled";
    /** 连电脑：采集推给电脑上的服务器，收 mp3 回来播（默认，行为和以前完全一致） */
    public static final String REMOTE_MODE = "remote";
    /** 手机独立：唤醒/识别/合成都在手机上，只有大模型联网，**不需要电脑** */
    public static final String LOCAL_MODE = "local";

    /** 服务状态变化会广播出去，供界面显示（MainActivity 注册接收）。 */
    public static final String ACTION_STATUS = "com.xiaoyin.remote.STATUS";
    public static final String EXTRA_STATUS = "status";
    /** 一轮对话内容（用户说的 / 小音回的），供界面记录 */
    public static final String ACTION_DIALOGUE = "com.xiaoyin.remote.DIALOGUE";
    public static final String EXTRA_WHO = "who";
    public static final String EXTRA_TEXT = "text";
    public static final String ACTION_STATE = "com.xiaoyin.remote.STATE";
    public static final String EXTRA_RUNNING = "running";

    private static final String CHANNEL_ID = "xiaoyin_remote";
    private static final int NOTIFY_ID = 1001;
    private static final long RETRY_MS = 3000;
    private static final String PREFS = "xiaoyin";
    private static final String KEY_HOST = "host";
    private static final String KEY_MODE = "voice_mode";

    private final Handler handler = new Handler(Looper.getMainLooper());
    private OkHttpClient client;
    private WebSocket socket;
    private String host;          // 形如 192.168.1.7:7860
    private String url;           // 设备连接的完整地址
    private boolean running;
    /** 设备通道当前是不是通的（状态的唯一真相）。界面用它显示"连上没连上"。 */
    public static volatile boolean isConnected = false;
    private boolean connected;
    /** 当前模式（remote / local），存 SharedPreferences 让重启后还在 */
    public static volatile String voiceMode = REMOTE_MODE;
    /** 端侧调试读数（噪声底/门限/音源/KWS 命中…），界面直接显示，见 LocalVoiceEngine.updateDebug */
    public static volatile String debugText = "";
    private int failures;         // 连续失败次数：用来决定要不要把排查提示也显示出来
    /** 当前语音链路（连电脑 / 手机独立），只会有其中一个活着 */
    private VoiceEngine voice;

    // 界面回到前台时靠这两个静态字段恢复显示——广播只在界面活着时收得到，
    // 连接常常是在界面切到后台之后才成功的（否则界面会一直停在"正在连接"）
    public static volatile String lastStatus = "还没连接";
    public static volatile boolean isRunning = false;
    public static volatile boolean isVoiceOn = false;
    public static volatile boolean lastContinuous = false;

    /**
     * 状态球该显示成什么样：off / idle（在听）/ wake（听到唤醒词）/ think（在想）
     * / speak（在说）/ error。界面靠它上色。
     */
    public static volatile String orbState = "off";

    /** 界面上的对话记录（{"我"/"小音", 内容}）。放静态是为了切回前台还能看到之前的几句。 */
    private static final Deque<String[]> CHAT = new ArrayDeque<>();
    private static final int CHAT_MAX = 200;
    /** 每变一次加一：界面靠它判断"要不要重画"，不用每几百毫秒重建一遍气泡 */
    private static volatile int chatVersion = 0;

    public static synchronized void recordDialogue(String who, String text) {
        CHAT.addLast(new String[]{who, text});
        while (CHAT.size() > CHAT_MAX) {
            CHAT.removeFirst();
        }
        chatVersion++;
    }

    public static int chatVersion() {
        return chatVersion;
    }

    /** 给界面用的一份拷贝（从旧到新）。 */
    public static synchronized List<String[]> chatSnapshot() {
        return new ArrayList<>(CHAT);
    }

    public static synchronized void clearChat() {
        CHAT.clear();
        chatVersion++;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        client = new OkHttpClient.Builder()
                .pingInterval(20, TimeUnit.SECONDS)   // 保活：也让服务端知道我们还连着
                .build();
        // 记住上次的地址：被系统杀掉后 START_STICKY 重启时 intent 是空的，
        // 没有这一步就只能干等用户手动再点一次"连接"
        String saved = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_HOST, null);
        if (saved != null && !saved.trim().isEmpty()) {
            host = saved.trim();
            url = "ws://" + host + "/ws/device";
        }
        // 模式同样要记：不然被系统杀掉重启后会莫名其妙回到"连电脑"
        String mode = getSharedPreferences(PREFS, MODE_PRIVATE).getString(KEY_MODE, REMOTE_MODE);
        voiceMode = LOCAL_MODE.equals(mode) ? LOCAL_MODE : REMOTE_MODE;
    }

    /**
     * 把用户填的地址整理成「主机:端口」。
     *
     * 最容易踩的一个坑：手机上装 APK 是从 {@code https://<IP>:7861} 下载的，于是顺手就把
     * 7861 填进来了 —— 但 7861 是给浏览器用的**加密**入口（TLS），App 走的是**明文** ws，
     * 要用主服务的 7860。这里直接纠正过来，省得对着一句「正在连接」猜半天。
     */
    public static String normalizeHost(String raw) {
        if (raw == null) {
            return null;
        }
        String h = raw.trim();
        for (String prefix : new String[]{"wss://", "ws://", "https://", "http://"}) {
            if (h.regionMatches(true, 0, prefix, 0, prefix.length())) {
                h = h.substring(prefix.length());
                break;
            }
        }
        int slash = h.indexOf('/');
        if (slash >= 0) {
            h = h.substring(0, slash);       // 去掉后面的路径
        }
        if (h.endsWith(":7861")) {
            h = h.substring(0, h.length() - ":7861".length()) + ":7860";
        } else if (!h.contains(":")) {
            h += ":7860";
        }
        return h.isEmpty() ? null : h;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : null;
        if (ACTION_STOP.equals(action)) {
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().remove(KEY_HOST).apply();
            stopRemote();
            return START_NOT_STICKY;
        }
        if (ACTION_VOICE.equals(action)) {
            // 语音助手开关：只影响语音会话，设备连接照旧（它一直在收播放指令）
            setVoice(intent.getBooleanExtra(EXTRA_ENABLED, false));
            return START_STICKY;
        }
        if (ACTION_MODE.equals(action)) {
            String m = intent.getStringExtra(EXTRA_MODE);
            if (LOCAL_MODE.equals(m) || REMOTE_MODE.equals(m)) {
                boolean wasOn = isVoiceOn;
                setVoice(false);                      // 换模式要先把旧链路停干净
                voiceMode = m;
                getSharedPreferences(PREFS, MODE_PRIVATE)
                        .edit().putString(KEY_MODE, m).apply();
                if (wasOn) {
                    setVoice(true);                   // 原来是开着的就按新模式重开
                } else {
                    status(LOCAL_MODE.equals(m)
                            ? "📴 已切到手机独立模式（不需要电脑）"
                            : "🔗 已切到连电脑模式");
                }
            }
            return START_STICKY;
        }
        if (ACTION_CONTINUOUS.equals(action)) {
            boolean on = intent.getBooleanExtra(EXTRA_ENABLED, false);
            if (voice != null) {
                voice.sendControl("{\"type\":\"continuous\",\"enabled\":" + on + "}");
            }
            lastContinuous = on;
            return START_STICKY;
        }
        String newHost = normalizeHost(intent != null ? intent.getStringExtra(EXTRA_HOST) : null);
        if (newHost != null) {
            host = newHost;
            url = "ws://" + host + "/ws/device";
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_HOST, host).apply();
        }
        if (url == null) {
            // 没填过地址 = 从没连过电脑。**本地模式要放行**，不能直接退出：
            // 手机独立模式压根不需要电脑，这里 stopRemote 会让"从没连过电脑的用户开不了机"
            if (LOCAL_MODE.equals(voiceMode)) {
                startForegroundWithType(isVoiceOn);
                broadcastState(true);
                status("📴 手机独立模式（不需要电脑）");
                return START_STICKY;
            }
            stopRemote();
            return START_NOT_STICKY;
        }
        startForegroundWithType(isVoiceOn);
        broadcastState(true);
        if (!running) {
            running = true;
            connect();
        }
        // 状态由服务说了算（界面不要自己编）：已经连着就把真实状态报回去，
        // 否则会一直停在界面自己贴的那句"正在连接"
        status(connected ? "✅ 已连上小音服务器" : "正在连接 " + host + " …");
        return START_STICKY;
    }

    private void connect() {
        if (!running || url == null) {
            return;
        }
        Request request = new Request.Builder().url(url).build();
        socket = client.newWebSocket(request, new WebSocketListener() {
            @Override
            public void onOpen(WebSocket webSocket, Response response) {
                failures = 0;
                setConnected(true);
                // 声明自己是设备端（服务端只用来记日志，指令方向是由它推给我们）
                webSocket.send("{\"type\":\"hello\",\"client\":\"android\"}");
                status("✅ 已连上小音服务器");
            }

            @Override
            public void onMessage(WebSocket webSocket, String text) {
                handle(text);
            }

            @Override
            public void onFailure(WebSocket webSocket, Throwable t, Response response) {
                setConnected(false);
                failures++;
                // 把失败原因带出来：光一句"正在连接"没法判断是地址错了、电脑没开还是被防火墙拦了
                String why = t.getClass().getSimpleName();
                status(failures >= 3
                        ? "⚠️ 连不上 " + host + "（" + why + "）—— 电脑上跑着 app.py 吗？"
                          + "防火墙放行过 7860 吗？"
                        : "⚠️ 连接失败（" + why + "），重试中…");
                retry();
            }

            @Override
            public void onClosed(WebSocket webSocket, int code, String reason) {
                setConnected(false);
                retry();
            }
        });
    }

    private void setConnected(boolean now) {
        connected = now;
        isConnected = now;
        broadcastState(running);     // 让界面那行"设备通道"跟着变
    }

    /** 把指令的执行结果回执给电脑（带上原指令的 id，让电脑那边能对上号）。 */
    private void replyResult(String id, boolean ok, String detail) {
        WebSocket ws = socket;
        if (ws == null || id == null || id.isEmpty()) {
            return;
        }
        try {
            JSONObject o = new JSONObject();
            o.put("type", "result");
            o.put("id", id);
            o.put("ok", ok);
            o.put("detail", detail == null ? "" : detail);
            ws.send(o.toString());
        } catch (Exception ignored) {
        }
    }

    private void retry() {
        if (!running) {
            return;
        }
        handler.postDelayed(this::connect, RETRY_MS);
    }

    private void handle(String text) {
        try {
            JSONObject msg = new JSONObject(text);
            String type = msg.optString("type");
            if ("play".equals(type)) {
                String songmid = msg.optString("songmid");
                String name = msg.optString("name");
                boolean ok = QQMusic.play(this, songmid);
                status(ok ? "▶ 正在播放《" + name + "》"
                          : "⚠️ 没能调起 QQ 音乐（装了吗？）");
                replyResult(msg.optString("id"), ok, ok ? "" : "没能调起 QQ 音乐（手机上装了吗？）");
            } else if ("media".equals(type)) {
                String action = msg.optString("action");
                MediaListener.Result r = MediaListener.control(action);
                status(r.ok ? "🎛 已发送 " + action : "⚠️ 没控制成功：" + r.detail);
                // 回执：电脑那边只知道"指令推出去了"，不回执它就会当成成功 ——
                // 然后 LLM 会兴高采烈地说"已经切到下一首啦"，而手机上根本没切（真发生过）
                replyResult(msg.optString("id"), r.ok, r.detail);
            }
        } catch (Exception ignored) {
            // 协议外的消息直接忽略
        }
    }

    /** 开/关语音助手（一直听着：说「小音」唤醒，还能连续对话）。 */
    private void setVoice(boolean enable) {
        isVoiceOn = enable;
        orbState = enable ? "idle" : "off";
        boolean local = LOCAL_MODE.equals(voiceMode);
        if (enable) {
            // 连电脑模式才需要地址；手机独立模式压根不碰电脑
            if (!local && host == null) {
                isVoiceOn = false;
                orbState = "off";      // 先改状态再发广播：界面收到时要已经是对的
                status("⚠️ 先连接服务器再开语音助手");
                return;
            }
            if (checkSelfPermission(Manifest.permission.RECORD_AUDIO)
                    != PackageManager.PERMISSION_GRANTED) {
                // 没授权就去声明 microphone 类型的话前台服务会被系统拒掉，整个连接都会断
                isVoiceOn = false;
                orbState = "off";
                status("⚠️ 请先允许麦克风权限，再开语音助手");
                return;
            }
            startForegroundWithType(true);      // 声明麦克风类型（要已授予 RECORD_AUDIO）
            if (voice == null) {
                voice = local ? newLocalEngine() : new VoiceClient(this, host, uiListener);
            }
            voice.start();
            status(local ? "📴 手机独立模式已开启" : "🎙 语音助手已开启");
        } else {
            if (voice != null) {
                voice.stop();
                voice = null;
            }
            debugText = "";
            startForegroundWithType(false);     // 退回纯数据同步类型
            status("语音助手已关闭");
        }
    }

    /** 两条链路共用同一套界面回调：所以切模式时界面一行都不用改 */
    private final VoiceClient.Listener uiListener = new VoiceClient.Listener() {
        @Override
        public void onStatus(String text) {
            status(text);
        }

        @Override
        public void onDialogue(String who, String text) {
            broadcastDialogue(who, text);
        }

        @Override
        public void onState(String state) {
            orbState = state;
        }
    };

    /**
     * 手机独立模式：唤醒/识别/合成都在手机上跑（见 {@code local.LocalVoiceEngine}）。
     *
     * <p>端侧依赖 sherpa-onnx 的原生库，万一加载不起来（缺 so/ABI 不对）会抛 {@link Throwable}
     * 而不是 Exception —— 所以这里 catch Throwable，并且**退回连电脑模式**，
     * 不能让端侧的问题把已经跑通的电脑链路一起拖死。
     */
    private VoiceEngine newLocalEngine() {
        try {
            return new com.xiaoyin.remote.local.LocalVoiceEngine(this, uiListener);
        } catch (Throwable t) {
            voiceMode = REMOTE_MODE;
            getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_MODE, REMOTE_MODE).apply();
            status("⚠️ 端侧引擎起不来（" + t.getClass().getSimpleName() + "），已退回连电脑模式");
            return new VoiceClient(this, host, uiListener);
        }
    }

    /**
     * 启动/更新前台服务，并**按需**声明类型。
     *
     * ⚠️ 这里是踩过的坑：清单里写了 microphone 类型，如果 startForeground 不显式指定类型，
     * 系统会按清单里的类型要求 App **此刻就持有 RECORD_AUDIO**；没授权直接抛异常、服务起不来，
     * 表现就是界面一直停在「正在连接」。所以：只用数据同步时只声明 dataSync，
     * 开语音助手（必然已授权）时才加上 microphone。
     */
    private void startForegroundWithType(boolean withMic) {
        Notification n = notification(lastStatus);
        try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
                if (withMic) {
                    type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE;
                }
                startForeground(NOTIFY_ID, n, type);
            } else {
                startForeground(NOTIFY_ID, n);
            }
        } catch (Exception e) {
            // 权限没给全（多半是麦克风）时别让服务直接崩，把原因说出来（也广播给界面）
            status("⚠️ 前台服务启动失败：" + e.getClass().getSimpleName());
        }
    }

    private void stopRemote() {
        if (voice != null) {
            voice.stop();
            voice = null;
        }
        isVoiceOn = false;
        orbState = "off";
        running = false;
        setConnected(false);
        handler.removeCallbacksAndMessages(null);
        if (socket != null) {
            try {
                socket.close(1000, "stop");
            } catch (Exception ignored) {
            }
            socket = null;
        }
        broadcastState(false);
        stopForeground(true);
        stopSelf();
    }

    @Override
    public void onDestroy() {
        running = false;
        handler.removeCallbacksAndMessages(null);
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    // ── 通知与状态广播 ──────────────────────────────────────

    private void status(String text) {
        lastStatus = text;
        // 转发给界面（对话/状态都要在 App 里看得见）
        Intent i = new Intent(ACTION_STATUS);
        i.putExtra(EXTRA_STATUS, text);
        sendBroadcast(i);
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null) {
            nm.notify(NOTIFY_ID, notification(text));
        }
    }

    /** 对话内容单独广播：界面上要能像聊天记录一样留下来（状态是会被覆盖的）。 */
    private void broadcastDialogue(String who, String text) {
        recordDialogue(who, text);      // 界面切后台时收不到广播，先存下来
        Intent i = new Intent(ACTION_DIALOGUE);
        i.putExtra(EXTRA_WHO, who);
        i.putExtra(EXTRA_TEXT, text);
        sendBroadcast(i);
    }

    private void broadcastState(boolean runningNow) {
        isRunning = runningNow;
        Intent i = new Intent(ACTION_STATE);
        i.putExtra(EXTRA_RUNNING, runningNow);
        sendBroadcast(i);
    }

    private Notification notification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm != null && nm.getNotificationChannel(CHANNEL_ID) == null) {
            NotificationChannel channel = new NotificationChannel(
                    CHANNEL_ID, "小音遥控", NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("保持与电脑上小音服务器的连接");
            nm.createNotificationChannel(channel);
        }
        PendingIntent open = PendingIntent.getActivity(
                this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        return new NotificationCompat.Builder(this, CHANNEL_ID)
                .setContentTitle("小音遥控")
                .setContentText(text)
                .setSmallIcon(R.drawable.ic_launcher)
                .setOngoing(true)
                .setContentIntent(open)
                .build();
    }
}
