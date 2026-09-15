package com.xiaoyin.remote;

import android.Manifest;
import android.animation.AnimatorSet;
import android.animation.ObjectAnimator;
import android.animation.ValueAnimator;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.text.TextUtils;
import android.view.LayoutInflater;
import android.view.View;
import android.view.animation.LinearInterpolator;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import androidx.appcompat.app.AppCompatActivity;
import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;

import java.util.List;

/**
 * 语音助手界面：上面一个会"呼吸"的状态球（在听／在想／在说三种颜色），中间是对话气泡，
 * 下面一个大按钮。服务器地址、权限这些收进「⚙ 设置」，默认不展开。
 *
 * ⚠️ 界面刷新靠 {@link #tick} 定时读服务里的静态状态，**不依赖广播**。
 * 之前用广播：服务在后台线程 sendBroadcast，界面在前台收不到，表现就是
 * 「对话不刷新，切出去再回来才更新」。同一个进程里直接读静态字段最稳。
 */
public class MainActivity extends AppCompatActivity {

    private static final String PREFS = "xiaoyin";
    private static final String KEY_HOST = "host";
    private static final long TICK_MS = 300;

    private EditText hostInput;
    private TextView statusText;
    private TextView connText;
    private TextView chatEmpty;
    private TextView permissionText;
    private LinearLayout chatList;
    private ScrollView chatScroll;
    private View orb;
    private View orbGlow;
    private TextView orbIcon;
    private View settingsPanel;
    private Button voiceBtn;
    private Button continuousBtn;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private AnimatorSet pulse;
    private String shownStatus;
    private String shownConn;
    private String shownOrbState;
    private boolean shownVoiceOn;
    private boolean shownContinuous;
    private int renderedChatVersion = -1;

    private final BroadcastReceiver receiver = new BroadcastReceiver() {
        @Override
        public void onReceive(Context context, Intent intent) {
            syncFromService();       // 广播能到就立刻刷，到不了也有定时兜底
        }
    };

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);

        hostInput = findViewById(R.id.host_input);
        statusText = findViewById(R.id.status_text);
        connText = findViewById(R.id.conn_text);
        chatEmpty = findViewById(R.id.chat_empty);
        permissionText = findViewById(R.id.permission_text);
        chatList = findViewById(R.id.chat_list);
        chatScroll = findViewById(R.id.chat_scroll);
        orb = findViewById(R.id.orb);
        orbGlow = findViewById(R.id.orb_glow);
        orbIcon = findViewById(R.id.orb_icon);
        settingsPanel = findViewById(R.id.settings_panel);
        voiceBtn = findViewById(R.id.voice_btn);
        continuousBtn = findViewById(R.id.continuous_btn);

        SharedPreferences prefs = getSharedPreferences(PREFS, MODE_PRIVATE);
        hostInput.setText(prefs.getString(KEY_HOST, ""));

        findViewById(R.id.settings_btn).setOnClickListener(v ->
                settingsPanel.setVisibility(
                        settingsPanel.getVisibility() == View.VISIBLE ? View.GONE : View.VISIBLE));

        findViewById(R.id.chat_clear_btn).setOnClickListener(v -> {
            RemoteService.clearChat();
            syncFromService();
        });

        findViewById(R.id.connect_btn).setOnClickListener(v -> connect());

        findViewById(R.id.stop_btn).setOnClickListener(v -> {
            startService(new Intent(this, RemoteService.class)
                    .setAction(RemoteService.ACTION_STOP));
            RemoteService.isRunning = false;
            RemoteService.isConnected = false;
            RemoteService.orbState = "off";
            syncFromService();
        });

        findViewById(R.id.discover_btn).setOnClickListener(v -> discover());

        voiceBtn.setOnClickListener(v -> toggleVoice());
        continuousBtn.setOnClickListener(v -> toggleContinuous());

        findViewById(R.id.overlay_btn).setOnClickListener(v -> {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.M && !Settings.canDrawOverlays(this)) {
                startActivity(new Intent(Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
                        Uri.parse("package:" + getPackageName())));
            } else {
                Toast.makeText(this, "已允许 ✓", Toast.LENGTH_SHORT).show();
            }
        });

        findViewById(R.id.notify_access_btn).setOnClickListener(v ->
                startActivity(new Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)));

        askNotificationPermission();
        applyOrbState("off");
        syncFromService();
    }

    @Override
    protected void onResume() {
        super.onResume();
        IntentFilter filter = new IntentFilter();
        filter.addAction(RemoteService.ACTION_STATUS);
        filter.addAction(RemoteService.ACTION_STATE);
        filter.addAction(RemoteService.ACTION_DIALOGUE);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(receiver, filter, Context.RECEIVER_NOT_EXPORTED);
        } else {
            registerReceiver(receiver, filter);
        }
        // 定时兜底：广播在有些 ROM/后台线程下收不到，就靠它保证界面一直在跟着服务变
        ui.removeCallbacks(tick);
        ui.post(tick);
        shownOrbState = null;        // 让状态球重新按当前状态起动画（onPause 时停了）
        refreshPermissions();
    }

    @Override
    protected void onPause() {
        super.onPause();
        ui.removeCallbacks(tick);
        stopPulse();                 // 界面看不见了就别烧电
        try {
            unregisterReceiver(receiver);
        } catch (IllegalArgumentException ignored) {
        }
    }

    @Override
    protected void onDestroy() {
        ui.removeCallbacksAndMessages(null);
        stopPulse();
        super.onDestroy();
    }

    // ── 定时同步：界面跟着服务里的真实状态走 ────────────────

    private final Runnable tick = new Runnable() {
        @Override
        public void run() {
            syncFromService();
            ui.postDelayed(this, TICK_MS);
        }
    };

    /** 把服务的真实状态反映到界面上；每项都先比对再 setText，避免无谓重绘、也避免打断动画。 */
    private void syncFromService() {
        String status = RemoteService.lastStatus;
        if (!TextUtils.equals(status, shownStatus)) {
            shownStatus = status;
            statusText.setText(status);
        }

        String conn;
        if (RemoteService.isConnected) {
            conn = getString(R.string.conn_up);
        } else if (RemoteService.isRunning) {
            conn = getString(R.string.conn_down);
        } else {
            conn = getString(R.string.conn_idle);
        }
        if (!TextUtils.equals(conn, shownConn)) {
            shownConn = conn;
            connText.setText(conn);
        }

        if (!TextUtils.equals(RemoteService.orbState, shownOrbState)) {
            applyOrbState(RemoteService.orbState);
        }

        if (RemoteService.isVoiceOn != shownVoiceOn) {
            shownVoiceOn = RemoteService.isVoiceOn;
            voiceBtn.setText(shownVoiceOn ? R.string.voice_stop : R.string.voice_start);
        }
        if (RemoteService.lastContinuous != shownContinuous) {
            shownContinuous = RemoteService.lastContinuous;
            continuousBtn.setText(shownContinuous
                    ? R.string.continuous_on : R.string.continuous_off);
        }

        if (renderedChatVersion != RemoteService.chatVersion()) {
            renderChat();
        }
    }

    // ── 状态球 ──────────────────────────────────────────────

    /** 在听=紫（慢慢呼吸）、在想=橙（快些）、在说=蓝（跟着说话节奏）、出错=红。 */
    private void applyOrbState(String state) {
        shownOrbState = state;
        int colorRes;
        int iconRes;
        long period;
        switch (state == null ? "off" : state) {
            case "idle":
                colorRes = R.color.orb_idle; iconRes = R.string.orb_idle; period = 1600; break;
            case "wake":
                colorRes = R.color.orb_wake; iconRes = R.string.orb_wake; period = 700; break;
            case "think":
                colorRes = R.color.orb_think; iconRes = R.string.orb_think; period = 900; break;
            case "speak":
                colorRes = R.color.orb_speak; iconRes = R.string.orb_speak; period = 520; break;
            case "error":
                colorRes = R.color.orb_error; iconRes = R.string.orb_error; period = 0; break;
            default:
                colorRes = R.color.orb_off; iconRes = R.string.orb_off; period = 0; break;
        }
        int color = ContextCompat.getColor(this, colorRes);
        orb.setBackgroundTintList(ColorStateList.valueOf(color));
        orbGlow.setBackgroundTintList(ColorStateList.valueOf(color));
        orbIcon.setText(iconRes);
        if (period > 0) {
            startPulse(period);
        } else {
            stopPulse();
        }
    }

    private void startPulse(long period) {
        stopPulse();
        ObjectAnimator scale = ObjectAnimator.ofPropertyValuesHolder(orb,
                android.animation.PropertyValuesHolder.ofFloat(View.SCALE_X, 1f, 1.12f),
                android.animation.PropertyValuesHolder.ofFloat(View.SCALE_Y, 1f, 1.12f));
        ObjectAnimator glow = ObjectAnimator.ofFloat(orbGlow, View.ALPHA, 0.15f, 0.55f);
        pulse = new AnimatorSet();
        pulse.playTogether(scale, glow);
        pulse.setDuration(period);
        pulse.setInterpolator(new LinearInterpolator());
        for (android.animation.Animator a : pulse.getChildAnimations()) {
            ((ObjectAnimator) a).setRepeatMode(ValueAnimator.REVERSE);
            ((ObjectAnimator) a).setRepeatCount(ValueAnimator.INFINITE);
        }
        pulse.start();
    }

    private void stopPulse() {
        if (pulse != null) {
            pulse.cancel();
            pulse = null;
        }
        orb.setScaleX(1f);
        orb.setScaleY(1f);
        orbGlow.setAlpha(0.35f);
    }

    // ── 对话气泡 ────────────────────────────────────────────

    private void renderChat() {
        renderedChatVersion = RemoteService.chatVersion();
        List<String[]> log = RemoteService.chatSnapshot();
        chatList.removeAllViews();
        boolean empty = log.isEmpty();
        chatEmpty.setVisibility(empty ? View.VISIBLE : View.GONE);
        chatScroll.setVisibility(empty ? View.GONE : View.VISIBLE);
        if (empty) {
            return;
        }
        LayoutInflater inflater = LayoutInflater.from(this);
        for (String[] line : log) {
            boolean mine = !"小音".equals(line[0]);
            View item = inflater.inflate(
                    mine ? R.layout.item_bubble_me : R.layout.item_bubble_ai, chatList, false);
            ((TextView) item.findViewById(R.id.bubble_text)).setText(line[1]);
            chatList.addView(item);
        }
        chatScroll.post(() -> chatScroll.fullScroll(View.FOCUS_DOWN));
    }

    // ── 交互 ────────────────────────────────────────────────

    private void connect() {
        // 整理成「主机:端口」：粘贴整条网址、漏了端口、或者填成浏览器的 7861 都能纠正
        String host = RemoteService.normalizeHost(hostInput.getText().toString());
        if (host == null) {
            Toast.makeText(this, "先填电脑的地址，例如 192.168.1.5:7860", Toast.LENGTH_LONG).show();
            return;
        }
        hostInput.setText(host);        // 把纠正后的地址显示出来，改了什么一眼能看到
        getSharedPreferences(PREFS, MODE_PRIVATE).edit().putString(KEY_HOST, host).apply();
        Intent intent = new Intent(this, RemoteService.class)
                .setAction(RemoteService.ACTION_START)
                .putExtra(RemoteService.EXTRA_HOST, host);
        ContextCompat.startForegroundService(this, intent);
        // 只做即时反馈，**不写** RemoteService.lastStatus：真实状态一律由服务说了算
        statusText.setText("正在连接 " + host + " …");
        shownStatus = null;
    }

    /**
     * 语音助手开关：打开后麦克风常开（前台服务 + microphone 类型，锁屏也能继续听），
     * 说「小音」唤醒、说完自动结束。没授麦克风权限就先申请。
     */
    private void toggleVoice() {
        boolean enable = !RemoteService.isVoiceOn;
        if (enable && ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.RECORD_AUDIO}, 2);
            Toast.makeText(this, "允许麦克风权限后再点一次「开始对话」", Toast.LENGTH_LONG).show();
            return;
        }
        startService(new Intent(this, RemoteService.class)
                .setAction(RemoteService.ACTION_VOICE)
                .putExtra(RemoteService.EXTRA_ENABLED, enable));
        RemoteService.isVoiceOn = enable;
        RemoteService.orbState = enable ? "idle" : "off";
        syncFromService();
    }

    private void toggleContinuous() {
        boolean on = !RemoteService.lastContinuous;
        startService(new Intent(this, RemoteService.class)
                .setAction(RemoteService.ACTION_CONTINUOUS)
                .putExtra(RemoteService.EXTRA_ENABLED, on));
        RemoteService.lastContinuous = on;
        syncFromService();
    }

    /**
     * 局域网自动发现：广播找电脑上的小音服务器，找到就自动填地址并连上。
     * 电脑换了网络、IP 变了也能找到（回复包的来源地址就是它的 IP），不用手改配置。
     */
    private void discover() {
        statusText.setText("正在局域网里找小音服务器…");
        shownStatus = null;
        new Thread(() -> {
            String found = Discovery.find();
            runOnUiThread(() -> {
                if (found == null) {
                    statusText.setText("没找到 —— 确认电脑上 python app.py 在跑，"
                            + "并且手机和电脑连的是同一个 WiFi");
                    shownStatus = null;
                    return;
                }
                String host = RemoteService.normalizeHost(found);
                hostInput.setText(host);
                getSharedPreferences(PREFS, MODE_PRIVATE)
                        .edit().putString(KEY_HOST, host).apply();
                Intent intent = new Intent(this, RemoteService.class)
                        .setAction(RemoteService.ACTION_START)
                        .putExtra(RemoteService.EXTRA_HOST, host);
                ContextCompat.startForegroundService(this, intent);
                statusText.setText("找到服务器：" + host + "，正在连接…");
                shownStatus = null;
            });
        }).start();
    }

    /** 把两项关键权限的当前状态显示出来——它们没授予时，功能会静默失效。 */
    private void refreshPermissions() {
        boolean overlay = Build.VERSION.SDK_INT < Build.VERSION_CODES.M
                || Settings.canDrawOverlays(this);
        boolean notifyAccess = MediaListener.available();
        boolean mic = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
                == PackageManager.PERMISSION_GRANTED;
        StringBuilder sb = new StringBuilder();
        sb.append(overlay ? "✅" : "❌").append(" 显示在其他应用上层（调起 QQ 音乐必需）\n");
        sb.append(notifyAccess ? "✅" : "❌").append(" 通知使用权（暂停/切歌必需）\n");
        sb.append(mic ? "✅" : "❌").append(" 麦克风（语音助手必需）");
        permissionText.setText(sb.toString());
    }

    private void askNotificationPermission() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
                && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this,
                    new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
    }
}
