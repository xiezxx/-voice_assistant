package com.xiaoyin.remote;

import android.content.Context;
import android.media.session.MediaController;
import android.media.session.MediaSessionManager;
import android.service.notification.NotificationListenerService;

import java.util.List;

/**
 * 媒体控制（暂停/继续/上一首/下一首）。
 *
 * QQ 音乐没有公开的控制协议，所以不用界面自动化那套，改用 Android 提供的标准接口：
 * 拿到「通知使用权」后，MediaSessionManager 就能看到 QQ 音乐的媒体会话，直接指挥它。
 *
 * 代价是用户要在系统设置里授予一次「通知使用权」（见 MainActivity 的引导按钮）——
 * 这是系统对媒体控制的硬要求，绕不开。
 */
public class MediaListener extends NotificationListenerService {

    private static MediaListener instance;

    @Override
    public void onListenerConnected() {
        instance = this;
    }

    @Override
    public void onListenerDisconnected() {
        if (instance == this) {
            instance = null;
        }
    }

    /** 用户是否已授予「通知使用权」。 */
    public static boolean available() {
        return instance != null;
    }

    /**
     * 对正在播放的 QQ 音乐执行一次控制。
     *
     * @param action pause / resume / next / prev
     * @return 是否找到 QQ 音乐的会话并发出控制
     */
    public static boolean control(String action) {
        MediaListener self = instance;
        if (self == null) {
            return false;   // 还没授权，或服务没连上
        }
        try {
            MediaSessionManager manager =
                    (MediaSessionManager) self.getSystemService(Context.MEDIA_SESSION_SERVICE);
            if (manager == null) {
                return false;
            }
            List<MediaController> sessions = manager.getActiveSessions(null);
            for (MediaController controller : sessions) {
                if (!QQMusic.PACKAGE.equals(controller.getPackageName())) {
                    continue;
                }
                MediaController.TransportControls t = controller.getTransportControls();
                switch (action) {
                    case "pause": t.pause(); return true;
                    case "resume": t.play(); return true;
                    case "next": t.skipToNext(); return true;
                    case "prev": t.skipToPrevious(); return true;
                    default: return false;
                }
            }
        } catch (Exception e) {
            return false;
        }
        return false;   // QQ 音乐当前没有活动会话（没在播）
    }
}
