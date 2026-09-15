package com.xiaoyin.remote;

import org.json.JSONObject;

import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InterfaceAddress;
import java.net.NetworkInterface;
import java.net.SocketTimeoutException;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/**
 * 局域网自动发现：广播找小音服务器，省掉手输 IP。
 *
 * 电脑上的 app.py 会听这个广播并回一条 JSON（含端口）。**关键点是：回复包的来源地址
 * 就是服务器 IP** —— 所以电脑换了网络、IP 变了，这里也能自己找到，不用改配置。
 */
public final class Discovery {

    private static final String PROBE = "XIAOYIN-DISCOVER";   // 与 discovery.py 里一致
    private static final int UDP_PORT = 7863;

    private Discovery() {
    }

    /** 广播找服务器。找到返回 "主机:端口"（例如 192.168.1.7:7860），找不到返回 null。 */
    public static String find() {
        DatagramSocket socket = null;
        try {
            socket = new DatagramSocket();
            socket.setBroadcast(true);
            socket.setSoTimeout(700);
            byte[] probe = PROBE.getBytes("UTF-8");

            // 广播几轮：UDP 会丢包，一轮不够稳
            for (int round = 0; round < 3; round++) {
                for (String target : broadcastTargets()) {
                    try {
                        socket.send(new DatagramPacket(probe, probe.length,
                                InetAddress.getByName(target), UDP_PORT));
                    } catch (Exception ignored) {
                        // 某个网卡广播不出去就跳过
                    }
                }
                long deadline = System.currentTimeMillis() + 900;
                while (System.currentTimeMillis() < deadline) {
                    byte[] buf = new byte[512];
                    DatagramPacket reply = new DatagramPacket(buf, buf.length);
                    try {
                        socket.receive(reply);
                    } catch (SocketTimeoutException e) {
                        break;      // 本轮没等到，进入下一轮广播
                    }
                    String text = new String(reply.getData(), 0, reply.getLength(), "UTF-8");
                    JSONObject obj = new JSONObject(text);
                    if ("xiaoyin".equals(obj.optString("service"))) {
                        int port = obj.optInt("ws_port", 7860);
                        // 回复包的来源地址 = 服务器 IP，所以 IP 变了也能对上
                        return reply.getAddress().getHostAddress() + ":" + port;
                    }
                }
            }
        } catch (Exception ignored) {
            // 没网卡、被系统拦等，都当作"没找到"
        } finally {
            if (socket != null) {
                socket.close();
            }
        }
        return null;
    }

    /** 广播地址：全局广播 + 各网卡自己的子网广播（有些 ROM 会拦 255.255.255.255）。 */
    private static List<String> broadcastTargets() {
        List<String> targets = new ArrayList<>();
        targets.add("255.255.255.255");
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) {
                    continue;
                }
                for (InterfaceAddress ia : ni.getInterfaceAddresses()) {
                    InetAddress broadcast = ia.getBroadcast();
                    if (broadcast != null) {
                        targets.add(broadcast.getHostAddress());
                    }
                }
            }
        } catch (Exception ignored) {
        }
        return targets;
    }
}
