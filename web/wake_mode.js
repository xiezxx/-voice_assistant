/* Web 免提唤醒 —— 浏览器端麦克风采集 + WebSocket 音频流 + 能量门 VAD 录音。
 *
 * 状态机：IDLE → LISTENING → RECORDING → UPLOADING → LISTENING
 *  - LISTENING：音频帧发往 /ws/wake，服务器 sherpa-onnx KWS 检测「小音」
 *  - RECORDING：收到唤醒消息后录下问题，VAD 参数与 audio_utils.py 对齐
 *  - UPLOADING：WAV 上传 /api/wake_audio，服务器跑完整管线（期间不监听）
 *
 * 依赖 DOM：#wake-btn（开关按钮）、#wake-status（状态文字）。
 * 注：ScriptProcessorNode 已标记废弃但全浏览器支持、代码量小；未来可换 AudioWorklet。
 */
(function () {
  'use strict';

  var CHUNK = 1600;        // 每帧样本数（0.1s @ 16k）
  var THR = 0.02;          // VAD 能量阈值（对齐 audio_utils.py 的 mean-abs）
  var SIL_FRAMES = 10;     // 1.0s 静音判定
  var MAX_SEC = 12;        // 最长录音
  var MIN_SEC = 0.3;       // 最短有效录音

  var state = 'IDLE';      // IDLE | LISTENING | RECORDING | UPLOADING
  var ws = null;
  var wsRetry = 0;
  var audioCtx = null;
  var micStream = null;
  var processor = null;
  var ring = [];           // 浮点采样缓冲，凑满 CHUNK 发一帧
  var rec = [], frames = 0, hasSpeech = false, silence = 0;

  var statusEl = document.getElementById('wake-status');

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function beep(freqs, dur) {
    try {
      var t0 = audioCtx.currentTime;
      freqs.forEach(function (f, i) {
        var osc = audioCtx.createOscillator();
        var gain = audioCtx.createGain();
        osc.frequency.value = f;
        osc.type = 'sine';
        gain.gain.setValueAtTime(0.25, t0);
        gain.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
        osc.connect(gain).connect(audioCtx.destination);
        osc.start(t0 + i * 0.05);
        osc.stop(t0 + dur + i * 0.05);
      });
    } catch (e) { /* 提示音失败不影响主流程 */ }
  }

  function connectWS() {
    var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(proto + location.host + '/ws/wake');
    ws.binaryType = 'arraybuffer';
    ws.onopen = function () {
      wsRetry = 0;
      if (state === 'LISTENING') setStatus('🟢 免提聆听中 — 说「小音」唤醒');
    };
    ws.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === 'wake' && state === 'LISTENING') {
        beep([1320, 1760], 0.18);
        setStatus('🔔 唤醒成功 — 请说话，说完停顿约 1 秒');
        rec = []; frames = 0; hasSpeech = false; silence = 0;
        state = 'RECORDING';
      } else if (m.type === 'error') {
        setStatus('⚠️ 唤醒服务不可用：' + (m.error || '') + '，请用麦克风按钮');
      }
    };
    ws.onclose = function () {
      ws = null;
      if (state !== 'IDLE') {
        wsRetry++;
        setStatus('🔌 连接断开，重连中…（第 ' + wsRetry + ' 次）');
        setTimeout(connectWS, 2000);
      }
    };
  }

  function toInt16(f32) {
    var i16 = new Int16Array(f32.length);
    for (var i = 0; i < f32.length; i++) {
      var s = Math.max(-1, Math.min(1, f32[i]));
      var v = Math.round(s * 32768);   // 与服务器解码（/32768）对齐
      i16[i] = Math.max(-32768, Math.min(32767, v));
    }
    return i16;
  }

  function sendToWs(f32) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(toInt16(f32).buffer);
  }

  function onChunk(f32) {
    if (state === 'LISTENING') sendToWs(f32);
    else if (state === 'RECORDING') recordFrame(f32);
  }

  function recordFrame(f32) {
    rec.push.apply(rec, f32);
    frames++;
    var sum = 0;
    for (var i = 0; i < f32.length; i++) sum += Math.abs(f32[i]);
    var level = sum / f32.length;
    if (!hasSpeech) {
      if (level >= THR) hasSpeech = true;
      return;
    }
    if (level < THR) {
      silence++;
      if (silence >= SIL_FRAMES && frames > MIN_SEC * 10) { finish(); return; }
    } else {
      silence = 0;
    }
    if (frames >= MAX_SEC * 10) finish();
  }

  function encodeWav(f32) {
    var n = f32.length;
    var buf = new ArrayBuffer(44 + n * 2);
    var v = new DataView(buf);
    var writeStr = function (off, s) {
      for (var i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
    };
    writeStr(0, 'RIFF');
    v.setUint32(4, 36 + n * 2, true);
    writeStr(8, 'WAVE');
    writeStr(12, 'fmt ');
    v.setUint32(16, 16, true);            // fmt 块长度
    v.setUint16(20, 1, true);             // PCM
    v.setUint16(22, 1, true);             // 单声道
    v.setUint32(24, 16000, true);         // 采样率
    v.setUint32(28, 32000, true);         // 字节率
    v.setUint16(32, 2, true);             // 块对齐
    v.setUint16(34, 16, true);            // 位深
    writeStr(36, 'data');
    v.setUint32(40, n * 2, true);
    var i16 = toInt16(f32);
    for (var i = 0; i < n; i++) {
      v.setInt16(44 + i * 2, i16[i], true);
    }
    return buf;
  }

  async function finish() {
    if (frames < MIN_SEC * 10) {
      setStatus('🟢 免提聆听中（未检测到有效语音，请再说一次）');
      state = 'LISTENING';
      rec = []; frames = 0; hasSpeech = false; silence = 0;
      return;
    }
    state = 'UPLOADING';
    setStatus('🔄 已录 ' + (frames / 10).toFixed(1) + ' 秒，上传识别中…');
    var wav = encodeWav(Float32Array.from(rec));
    try {
      var r = await fetch('/api/wake_audio', {
        method: 'POST',
        headers: { 'Content-Type': 'audio/wav' },
        body: wav,
      });
      var j = await r.json();
      // 注意：服务器处理完（含 LLM 回复 + TTS）才会返回，回复由页面定时器逐句展示
      if (j.ok && j.empty) setStatus('⚠️ 未识别到语音，请再说一次');
      else if (!j.ok) setStatus('⚠️ 处理失败：' + (j.error || ''));
      else setStatus('🤖 已识别：「' + j.user_text + '」— 回复马上就来');
    } catch (e) {
      setStatus('⚠️ 上传失败：' + e);
    }
    rec = []; frames = 0; hasSpeech = false; silence = 0;
    state = 'LISTENING';
  }

  function stopWakeMode() {
    state = 'IDLE';
    if (ws) { try { ws.close(); } catch (e) {} ws = null; }
    if (processor) { try { processor.disconnect(); } catch (e) {} processor = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    if (audioCtx) { try { audioCtx.close(); } catch (e) {} audioCtx = null; }
    ring = []; rec = []; frames = 0; hasSpeech = false; silence = 0;
    setStatus('免提唤醒已关闭');
  }

  function startWakeMode() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      setStatus('⚠️ 当前浏览器不支持麦克风采集，请使用 Chrome/Edge');
      return;
    }
    navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    }).then(function (s) {
      micStream = s;
      audioCtx = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 16000 });
      audioCtx.resume();
      beep([880, 1320], 0.15);            // 开启提示音（用户手势内，解锁自动播放）
      connectWS();
      var src = audioCtx.createMediaStreamSource(s);
      processor = audioCtx.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = function (ev) {
        var f32 = ev.inputBuffer.getChannelData(0);
        ring.push.apply(ring, f32);
        while (ring.length >= CHUNK) {
          var chunk = ring.splice(0, CHUNK);
          onChunk(Float32Array.from(chunk));
        }
      };
      src.connect(processor);
      processor.connect(audioCtx.destination);  // 必须连 destination 才会回调
      state = 'LISTENING';
      setStatus('🟢 免提聆听中 — 说「小音」唤醒');
    }).catch(function (e) {
      var hint = e.name === 'NotAllowedError' ? '请在浏览器地址栏允许麦克风权限后重试'
        : e.name === 'NotFoundError' ? '未检测到麦克风设备'
        : e.name;
      setStatus('⚠️ 无法访问麦克风（' + hint + '）。手动点击麦克风按钮仍可对话');
    });
  }

  function wakeToggle() {
    if (state === 'IDLE') startWakeMode();
    else stopWakeMode();
  }

  // 按钮点击由 Gradio 的 click 事件（js 参数）调用本函数，勿在此重复绑定防双重切换
  window.wakeToggle = wakeToggle;
  window.addEventListener('beforeunload', function () {
    if (state !== 'IDLE') stopWakeMode();
  });
})();
