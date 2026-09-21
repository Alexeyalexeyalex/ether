/* ЭФИР — голос и трансляция экрана.
 *
 * Схема: полносвязная сетка (mesh). Каждый участник голосового канала держит
 * по одному RTCPeerConnection с каждым другим. Сервер только передаёт SDP и ICE.
 *
 * Важная деталь: в каждом соединении заранее создаются три дорожки в жёстком
 * порядке — микрофон (mid 0), звук экрана (mid 1), картинка экрана (mid 2).
 * Благодаря этому включение трансляции — это replaceTrack без пересогласования,
 * то есть без «шторок» и гонок между участниками.
 */

(function (global) {
  'use strict';

  // Получаем TURN с сервера при каждом входе: временные пароли не зашиты в JS.
  let ICE_CONFIG = {
    iceServers: [
      { urls: ['stun:stun.l.google.com:19302', 'stun:stun1.l.google.com:19302'] },
      { urls: 'stun:stun.cloudflare.com:3478' }
    ],
    iceCandidatePoolSize: 0
  };
  const earlyIce = new Map();
  const signalQueues = new Map();
  const blockedAudio = new Set();
  let joinGeneration = 0;

  const QUALITY = {
    saver: {
      label: 'Экономный', detail: '1280×720 · 15 к/с · 0,8 Мбит/с',
      width: 1280, height: 720, fps: 15, bitrate: 800000, bars: 1, hint: 'detail'
    },
    standard: {
      label: 'Стандартный', detail: '1920×1080 · 30 к/с · 2,5 Мбит/с',
      width: 1920, height: 1080, fps: 30, bitrate: 2500000, bars: 2, hint: 'detail'
    },
    high: {
      label: 'Высокий', detail: '1920×1080 · 60 к/с · 6 Мбит/с',
      width: 1920, height: 1080, fps: 60, bitrate: 6000000, bars: 3, hint: 'motion'
    },
    ultra: {
      label: 'Максимальный', detail: '2560×1440 · 60 к/с · 12 Мбит/с',
      width: 2560, height: 1440, fps: 60, bitrate: 12000000, bars: 4, hint: 'motion'
    },
    source: {
      label: 'Как в источнике', detail: 'Родное разрешение · 60 к/с · 20 Мбит/с',
      width: null, height: null, fps: 60, bitrate: 20000000, bars: 4, hint: 'motion'
    }
  };

  const RTC = {
    QUALITY,
    quality: 'high',
    channelId: null,
    socket: null,
    selfSid: null,
    micStream: null,
    micEnabled: true,
    screenStream: null,
    screenTrack: null,
    screenAudioTrack: null,   // то, что уходит участникам (после обработки)
    screenAudioRaw: null,     // как пришло от браузера
    screenSurface: null,      // monitor | window | browser
    screenAudioMode: 'duck',  // off | duck | raw
    duckNode: null,
    refMix: null,
    refAnalyser: null,
    peers: new Map(),
    handlers: {},
    audioBin: null,
    micGain: 1,            // усиление своего микрофона, 0..2
    micGainNode: null,
    micGateNode: null,     // шумовой порог
    micDenoiseNode: null,  // шумодав
    micDenoise: 1,         // интенсивность шумодава, 0..2
    micAnalyser: null,
    micThreshold: 0.045,   // ниже этого уровня микрофон не передаёт
    localLevel: 0,         // текущий уровень входа, 0..1
    rawMicStream: null,    // до обработки — им же и глушим дорожку
    volumes: {},           // user_id -> громкость воспроизведения, 0..2
    silenced: {},          // user_id -> true, если действует тишина
    peerUsers: {},         // sid -> user_id
    handUp: false,
    joining: false,
    hasTurn: false,
    lastError: ''
  };

  function emit(name, detail) {
    const fn = RTC.handlers[name];
    if (typeof fn === 'function') {
      try { fn(detail); } catch (err) { console.error(name, err); }
    }
  }

  RTC.on = function (name, fn) { RTC.handlers[name] = fn; };

  // ------------------------------------------------------------- звук

  function audioBin() {
    if (!RTC.audioBin) {
      RTC.audioBin = document.createElement('div');
      RTC.audioBin.style.display = 'none';
      document.body.appendChild(RTC.audioBin);
    }
    return RTC.audioBin;
  }

  const boosts = new Map();   // элемент -> {source, gain}
  const refTaps = new Map();  // элемент -> {source, gain}

  /* Узел-приёмник для всего, что звучит у нас: голоса участников и их
     трансляции. Никуда дальше не подключён — только измеряем уровень.
     Нужен, чтобы понимать, когда чужая речь попадёт в захват системного
     звука, и вовремя приглушить дорожку экрана. */
  function referenceMix() {
    const ctx = ensureCtx();
    if (!ctx) return null;
    if (!RTC.refMix) {
      RTC.refMix = ctx.createGain();
      RTC.refAnalyser = ctx.createAnalyser();
      RTC.refAnalyser.fftSize = 512;
      RTC.refMix.connect(RTC.refAnalyser);
    }
    return RTC.refMix;
  }

  function tapForReference(el) {
    const ctx = ensureCtx();
    const mix = referenceMix();
    if (!ctx || !mix || refTaps.has(el) || !el.srcObject) return;
    try {
      const source = ctx.createMediaStreamSource(el.srcObject);
      const gain = ctx.createGain();
      gain.gain.value = 1;
      source.connect(gain);
      gain.connect(mix);
      refTaps.set(el, { source: source, gain: gain });
    } catch (err) { /* без ответвления просто не будет приглушения */ }
  }

  function dropReference(el) {
    const tap = refTaps.get(el);
    if (!tap) return;
    try { tap.source.disconnect(); tap.gain.disconnect(); } catch (err) { /* уже отключено */ }
    refTaps.delete(el);
  }

  function playTrack(sid, kind, track) {
    const el = document.createElement('audio');
    el.autoplay = true;
    el.playsInline = true;
    el.dataset.sid = sid;
    el.dataset.kind = kind;
    el.dataset.user = String(RTC.peerUsers[sid] || '');
    el.srcObject = new MediaStream([track]);
    audioBin().appendChild(el);
    el.play().catch(function (err) {
      if (!el.isConnected || err.name !== 'NotAllowedError') return;
      blockedAudio.add(el);
      emit('playback', { blocked: true });
    });
    tapForReference(el);
    applyVolume(el);
    return el;
  }

  RTC.resumeAudio = async function () {
    const ctx = ensureCtx();
    if (ctx && ctx.state === 'suspended') await ctx.resume();
    await Promise.all(Array.from(blockedAudio).map(async function (el) {
      if (!el.isConnected) { blockedAudio.delete(el); return; }
      try { await el.play(); blockedAudio.delete(el); } catch (err) { /* ждём жеста */ }
    }));
    emit('playback', { blocked: blockedAudio.size > 0 });
  };

  /* Громкость до 100% — штатным свойством элемента. Выше 100% элемент
     не умеет, поэтому звук уводится в WebAudio через усилитель. Если
     контекст недоступен, тихо остаёмся на 100% — лучше так, чем без звука. */
  function attachBoost(el, value) {
    const ctx = audioCtx;
    if (!ctx || ctx.state !== 'running' || !el.srcObject) return false;
    let chain = boosts.get(el);
    if (!chain) {
      try {
        const source = ctx.createMediaStreamSource(el.srcObject);
        const gain = ctx.createGain();
        source.connect(gain);
        gain.connect(ctx.destination);
        chain = { source: source, gain: gain };
        boosts.set(el, chain);
      } catch (err) {
        return false;
      }
    }
    chain.gain.gain.value = value;
    el.muted = true;
    return true;
  }

  function detachBoost(el) {
    const chain = boosts.get(el);
    if (!chain) return;
    try { chain.source.disconnect(); chain.gain.disconnect(); } catch (err) { /* уже отключено */ }
    boosts.delete(el);
  }

  function applyVolume(el) {
    const userId = el.dataset.user;
    const tap = refTaps.get(el);

    if (el.dataset.kind === 'mic' && RTC.silenced[userId]) {
      detachBoost(el);
      el.muted = true;
      if (tap) tap.gain.gain.value = 0;   // заглушённого не слышно, приглушать нечего
      return;
    }
    const value = RTC.volumes[userId] == null ? 1 : RTC.volumes[userId];
    if (tap) tap.gain.gain.value = value;
    if (value > 1 && attachBoost(el, value)) return;
    detachBoost(el);
    el.muted = false;
    el.volume = Math.max(0, Math.min(value, 1));
  }

  function refreshVolumes() {
    audioBin().querySelectorAll('audio').forEach(applyVolume);
  }

  RTC.setPeerVolume = function (userId, value) {
    RTC.volumes[String(userId)] = value;
    refreshVolumes();
  };

  RTC.getPeerVolume = function (userId) {
    const value = RTC.volumes[String(userId)];
    return value == null ? 1 : value;
  };

  /* Тишину включил модератор: заглушаем таких участников и на приём тоже,
     чтобы правило работало даже против правленого клиента. */
  RTC.setSilenced = function (userIds) {
    RTC.silenced = {};
    (userIds || []).forEach(function (id) { RTC.silenced[String(id)] = true; });
    refreshVolumes();
  };

  function dropAudio(sid) {
    audioBin().querySelectorAll('audio[data-sid="' + sid + '"]').forEach(function (el) {
      detachBoost(el);
      dropReference(el);
      blockedAudio.delete(el);
      el.srcObject = null;
      el.remove();
    });
  }

  // ------------------------------------------------------------- сигналы и уровень

  let audioCtx = null;

  function ensureCtx() {
    try {
      if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === 'suspended') audioCtx.resume().catch(function () {});
      return audioCtx;
    } catch (err) {
      return null;
    }
  }

  /* Короткие сигналы синтезируются на месте: ни файлов, ни загрузки,
     ни задержки на первое воспроизведение. */
  const SFX = {
    enabled: true,

    blip: function (freq, at, duration, volume, type) {
      const ctx = ensureCtx();
      if (!ctx || !SFX.enabled) return;
      try {
        const start = ctx.currentTime + at;
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.type = type || 'sine';
        osc.frequency.setValueAtTime(freq, start);
        gain.gain.setValueAtTime(0.0001, start);
        gain.gain.exponentialRampToValueAtTime(volume, start + 0.015);
        gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.start(start);
        osc.stop(start + duration + 0.03);
      } catch (err) { /* звук не критичен */ }
    },

    // Свой вход — две ноты вверх, выход — две вниз.
    join: function () {
      SFX.blip(587.33, 0, 0.16, 0.22);
      SFX.blip(880.00, 0.085, 0.20, 0.20);
    },
    leave: function () {
      SFX.blip(659.25, 0, 0.16, 0.20);
      SFX.blip(415.30, 0.085, 0.22, 0.17);
    },
    // Чужие приход и уход — тише и одной нотой, чтобы не перебивали разговор.
    peerIn: function () { SFX.blip(783.99, 0, 0.13, 0.10); },
    peerOut: function () { SFX.blip(523.25, 0, 0.15, 0.09); }
  };

  function watchLevel(sid, stream) {
    try {
      const ctx = ensureCtx();
      if (!ctx) return function () {};
      const source = ctx.createMediaStreamSource(stream);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      source.connect(analyser);
      const buffer = new Uint8Array(analyser.frequencyBinCount);
      let speaking = false;

      const timer = setInterval(function () {
        analyser.getByteTimeDomainData(buffer);
        let sum = 0;
        for (let i = 0; i < buffer.length; i++) {
          const v = (buffer[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / buffer.length);
        const now = rms > 0.045;
        if (now !== speaking) {
          speaking = now;
          emit('speaking', { sid: sid, speaking: now });
        }
      }, 220);

      return function () {
        clearInterval(timer);
        try { source.disconnect(); } catch (e) { /* уже отключено */ }
      };
    } catch (err) {
      return function () {};
    }
  }

  // ------------------------------------------------------------- соединения

  function connectionChanged() { emit('connection', RTC.connectionSummary()); }

  RTC.connectionSummary = function () {
    const peers = Array.from(RTC.peers.values());
    return {
      total: peers.length,
      connected: peers.filter(function (p) { return p.pc.connectionState === 'connected'; }).length,
      failed: peers.some(function (p) { return p.exhausted; }),
      blockedAudio: blockedAudio.size > 0,
      hasTurn: RTC.hasTurn
    };
  };

  function sendSignal(sid, payload) {
    if (RTC.channelId && RTC.socket.connected) {
      RTC.socket.emit('signal', { to: sid, channel_id: RTC.channelId, payload: payload });
    }
  }

  async function restartPeer(peer) {
    if (RTC.peers.get(peer.sid) !== peer || peer.pc.signalingState !== 'stable') return;
    if (peer.isOfferer) {
      const offer = await peer.pc.createOffer({ iceRestart: true });
      if (RTC.peers.get(peer.sid) !== peer) return;
      await peer.pc.setLocalDescription(offer);
      sendSignal(peer.sid, { type: 'offer', sdp: peer.pc.localDescription });
    } else {
      sendSignal(peer.sid, { type: 'restart' });
    }
  }

  function scheduleRecovery(peer, delay) {
    if (peer.recoveryTimer || peer.exhausted) return;
    peer.recoveryTimer = setTimeout(function () {
      peer.recoveryTimer = null;
      if (RTC.peers.get(peer.sid) !== peer || peer.pc.connectionState === 'connected') return;
      if (peer.restarts >= 2) {
        peer.exhausted = true;
        RTC.lastError = RTC.hasTurn
          ? 'Не удалось связаться с участником. Проверьте сеть и доступность TURN-сервера.'
          : 'Прямое соединение не удалось. Для связи между разными сетями администратору нужно настроить TURN.';
        emit('notice', { level: 'error', text: RTC.lastError });
        connectionChanged();
        return;
      }
      peer.restarts += 1;
      restartPeer(peer).catch(function (err) { console.warn('ICE restart', err); });
      scheduleRecovery(peer, 12000);
    }, delay);
  }

  RTC.reconnect = function () {
    RTC.lastError = '';
    RTC.peers.forEach(function (peer) {
      if (peer.pc.connectionState === 'connected') return;
      clearTimeout(peer.recoveryTimer);
      peer.recoveryTimer = null;
      peer.restarts = 0;
      peer.exhausted = false;
      scheduleRecovery(peer, 0);
    });
    connectionChanged();
  };

  RTC.diagnostics = async function () {
    const peers = await Promise.all(Array.from(RTC.peers.values()).map(async function (peer) {
      const stats = await peer.pc.getStats();
      const transport = Array.from(stats.values()).find(function (s) { return s.type === 'transport' && s.selectedCandidatePairId; });
      const pair = transport && stats.get(transport.selectedCandidatePairId);
      const candidate = pair && stats.get(pair.localCandidateId);
      const audio = Array.from(stats.values()).filter(function (s) { return s.type === 'inbound-rtp' && s.kind === 'audio'; });
      return {
        userId: RTC.peerUsers[peer.sid], state: peer.pc.connectionState, failed: peer.exhausted,
        ice: peer.pc.iceConnectionState, route: candidate ? candidate.candidateType : '—',
        packets: audio.reduce(function (n, s) { return n + s.packetsReceived; }, 0),
        energy: audio.reduce(function (n, s) { return n + (s.totalAudioEnergy || 0); }, 0),
        rtt: pair && pair.currentRoundTripTime != null ? Math.round(pair.currentRoundTripTime * 1000) : null
      };
    }));
    return { secure: window.isSecureContext, signaling: RTC.socket.connected,
      transport: RTC.socket.io.engine && RTC.socket.io.engine.transport.name,
      hasTurn: RTC.hasTurn, microphone: RTC.hasMic(), muted: !RTC.micEnabled,
      blockedAudio: blockedAudio.size > 0, peers: peers, lastError: RTC.lastError };
  };

  function preferCodec(transceiver) {
    if (!transceiver || !RTCRtpSender.getCapabilities) return;
    try {
      const caps = RTCRtpSender.getCapabilities('video');
      if (!caps) return;
      const rank = function (codec) {
        const name = codec.mimeType.toLowerCase();
        if (name.indexOf('av1') >= 0) return 0;
        if (name.indexOf('vp9') >= 0) return 1;
        if (name.indexOf('h264') >= 0) return 2;
        if (name.indexOf('vp8') >= 0) return 3;
        return 4;
      };
      const ordered = caps.codecs.slice().sort(function (a, b) { return rank(a) - rank(b); });
      transceiver.setCodecPreferences(ordered);
    } catch (err) { /* браузер не умеет — не страшно */ }
  }

  function createPeer(sid, isOfferer) {
    if (RTC.peers.has(sid)) return RTC.peers.get(sid);
    const pc = new RTCPeerConnection(ICE_CONFIG);
    const peer = {
      sid: sid,
      pc: pc,
      isOfferer: isOfferer,
      micTx: null,
      screenAudioTx: null,
      videoTx: null,
      audioSeen: 0,
      stopMeter: null,
      pendingIce: earlyIce.get(sid) || [],
      recoveryTimer: null,
      restarts: 0,
      exhausted: false
    };
    earlyIce.delete(sid);
    RTC.peers.set(sid, peer);

    pc.onicecandidate = function (event) {
      if (event.candidate) {
        sendSignal(sid, { type: 'ice', candidate: event.candidate });
      }
    };

    pc.onconnectionstatechange = function () {
      if (RTC.peers.get(sid) !== peer) return;
      if (pc.connectionState === 'connected') {
        clearTimeout(peer.recoveryTimer);
        peer.recoveryTimer = null;
        peer.restarts = 0;
        peer.exhausted = false;
        RTC.lastError = '';
      } else if (['failed', 'disconnected'].includes(pc.connectionState)) {
        scheduleRecovery(peer, pc.connectionState === 'failed' ? 500 : 4000);
      }
      connectionChanged();
    };

    pc.ontrack = function (event) {
      const track = event.track;
      const mid = event.transceiver && event.transceiver.mid;

      if (track.kind === 'video') {
        const stream = event.streams[0] || new MediaStream([track]);
        emit('screen', { sid: sid, stream: stream, track: track });
        track.onended = function () { emit('screen_off', { sid: sid }); };
        return;
      }

      // Первая аудиодорожка — микрофон, вторая — звук экрана.
      const isMic = mid === '0' || (mid === null && peer.audioSeen === 0);
      peer.audioSeen += 1;
      playTrack(sid, isMic ? 'mic' : 'screen', track);
      if (isMic && !peer.stopMeter) {
        peer.stopMeter = watchLevel(sid, new MediaStream([track]));
      }
    };

    if (isOfferer) {
      const micTrack = RTC.micStream ? RTC.micStream.getAudioTracks()[0] : null;
      peer.micTx = pc.addTransceiver(micTrack || 'audio', { direction: 'sendrecv' });
      peer.screenAudioTx = pc.addTransceiver(RTC.screenAudioTrack || 'audio', { direction: 'sendrecv' });
      peer.videoTx = pc.addTransceiver(RTC.screenTrack || 'video', { direction: 'sendrecv' });
      preferCodec(peer.videoTx);
    }

    scheduleRecovery(peer, 16000);
    connectionChanged();
    return peer;
  }

  async function bindAnswererTransceivers(peer) {
    const list = peer.pc.getTransceivers();
    peer.micTx = list[0] || null;
    peer.screenAudioTx = list[1] || null;
    peer.videoTx = list[2] || null;

    const micTrack = RTC.micStream ? RTC.micStream.getAudioTracks()[0] : null;
    if (peer.micTx) {
      peer.micTx.direction = 'sendrecv';
      await peer.micTx.sender.replaceTrack(micTrack);
    }
    if (peer.screenAudioTx) {
      peer.screenAudioTx.direction = 'sendrecv';
      if (RTC.screenAudioTrack) {
        await peer.screenAudioTx.sender.replaceTrack(RTC.screenAudioTrack);
      }
    }
    if (peer.videoTx) {
      peer.videoTx.direction = 'sendrecv';
      preferCodec(peer.videoTx);
      if (RTC.screenTrack) {
        await peer.videoTx.sender.replaceTrack(RTC.screenTrack);
      }
    }
  }

  async function offerTo(sid) {
    const peer = createPeer(sid, true);
    const offer = await peer.pc.createOffer();
    await peer.pc.setLocalDescription(offer);
    if (RTC.peers.get(sid) !== peer) return;
    sendSignal(sid, { type: 'offer', sdp: peer.pc.localDescription });
    applyQualityToPeer(peer);
  }

  async function handleSignal(from, payload) {
    if (!payload || !RTC.channelId) return;
    let peer = RTC.peers.get(from);

    if (payload.type === 'offer') {
      if (!peer) peer = createPeer(from, false);
      await peer.pc.setRemoteDescription(new RTCSessionDescription(payload.sdp));
      await bindAnswererTransceivers(peer);
      const answer = await peer.pc.createAnswer();
      await peer.pc.setLocalDescription(answer);
      sendSignal(from, { type: 'answer', sdp: peer.pc.localDescription });
      await flushIce(peer);
      applyQualityToPeer(peer);
      return;
    }

    if (!peer) {
      if (payload.type === 'ice' && payload.candidate) {
        const pending = earlyIce.get(from) || [];
        if (pending.length < 100) pending.push(payload.candidate);
        earlyIce.set(from, pending);
      }
      return;
    }

    if (payload.type === 'restart') {
      if (peer.isOfferer) await restartPeer(peer);
      return;
    }

    if (payload.type === 'answer') {
      await peer.pc.setRemoteDescription(new RTCSessionDescription(payload.sdp));
      await flushIce(peer);
      applyQualityToPeer(peer);
      return;
    }

    if (payload.type === 'ice' && payload.candidate) {
      if (!peer.pc.remoteDescription || !peer.pc.remoteDescription.type) {
        peer.pendingIce.push(payload.candidate);
      } else {
        try { await peer.pc.addIceCandidate(payload.candidate); } catch (err) { console.warn('ICE candidate', err); }
      }
    }
  }

  async function flushIce(peer) {
    while (peer.pendingIce.length) {
      const candidate = peer.pendingIce.shift();
      try { await peer.pc.addIceCandidate(candidate); } catch (err) { console.warn('Queued ICE candidate', err); }
    }
  }

  function closePeer(sid) {
    earlyIce.delete(sid);
    signalQueues.delete(sid);
    const peer = RTC.peers.get(sid);
    if (!peer) return;
    if (peer.stopMeter) peer.stopMeter();
    clearTimeout(peer.recoveryTimer);
    try { peer.pc.close(); } catch (err) { /* уже закрыто */ }
    RTC.peers.delete(sid);
    dropAudio(sid);
    emit('screen_off', { sid: sid });
    emit('peer_closed', { sid: sid });
    connectionChanged();
  }

  // ------------------------------------------------------------- качество

  function applyQualityToPeer(peer) {
    const preset = QUALITY[RTC.quality];
    if (!peer.videoTx || !peer.videoTx.sender) return;
    const sender = peer.videoTx.sender;
    let params;
    try { params = sender.getParameters(); } catch (err) { return; }
    if (!params.encodings || !params.encodings.length) params.encodings = [{}];
    params.encodings[0].maxBitrate = preset.bitrate;
    params.encodings[0].maxFramerate = preset.fps;
    params.encodings[0].scaleResolutionDownBy = 1;
    params.degradationPreference = preset.hint === 'detail' ? 'maintain-resolution' : 'balanced';
    try {
      sender.setParameters(params).catch(function () { /* применится после согласования */ });
    } catch (err) { /* соединение ещё не готово */ }
  }

  RTC.setQuality = async function (key) {
    if (!QUALITY[key]) return;
    RTC.quality = key;
    const preset = QUALITY[key];

    if (RTC.screenTrack) {
      const constraints = { frameRate: { max: preset.fps } };
      if (preset.width) {
        constraints.width = { max: preset.width };
        constraints.height = { max: preset.height };
      }
      try { await RTC.screenTrack.applyConstraints(constraints); } catch (err) { /* источник не позволил */ }
      RTC.screenTrack.contentHint = preset.hint;
    }

    RTC.peers.forEach(applyQualityToPeer);

    if (RTC.socket && RTC.channelId) {
      RTC.socket.emit('voice_state', { quality: key });
    }
    emit('quality', { quality: key });
  };

  // ------------------------------------------------------------- вход и выход

  /* Микрофон берём в несколько подходов: сначала с обработкой звука,
     потом без всяких условий. Если устройства нет вовсе — это не повод
     не пускать человека в канал, он будет слушать других. */
  async function grabMic() {
    const attempts = [
      {
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          channelCount: 1
        },
        video: false
      },
      { audio: true, video: false }
    ];

    let lastError = null;
    for (let i = 0; i < attempts.length; i++) {
      try {
        return await navigator.mediaDevices.getUserMedia(attempts[i]);
      } catch (err) {
        lastError = err;
        // запрет и небезопасный контекст вторая попытка не исправит
        if (err.name === 'NotAllowedError' || err.name === 'SecurityError') throw err;
      }
    }
    throw lastError || new Error('Микрофон недоступен');
  }

  RTC.describeMicError = function (err) {
    switch (err && err.name) {
      case 'NotAllowedError':
      case 'PermissionDeniedError':
        return 'Доступ к микрофону запрещён. Разрешите его в настройках сайта — ' +
               'значок слева от адреса в браузере.';
      case 'NotFoundError':
      case 'DevicesNotFoundError':
        return 'Микрофон не найден. Подключите гарнитуру и проверьте в параметрах ' +
               'Windows: «Звук» → «Ввод», а также «Конфиденциальность» → «Микрофон».';
      case 'NotReadableError':
      case 'TrackStartError':
        return 'Микрофон занят другой программой. Закройте её и нажмите кнопку микрофона.';
      case 'OverconstrainedError':
        return 'Микрофон не поддерживает запрошенные параметры записи.';
      case 'SecurityError':
        return 'Браузер запретил микрофон: нужен адрес https:// или localhost.';
      default:
        return (err && err.message) || 'Не удалось включить микрофон';
    }
  };

  RTC.hasMic = function () { return !!RTC.micStream; };

  /* Пропускаем микрофон через регулятор громкости. Если WebAudio недоступен,
     отдаём поток как есть — связь важнее регулировки. */
  /* Модуль шумодава грузится один раз и живёт до конца сессии.
     Если браузер не умеет AudioWorklet, работаем без него. */
  let denoiseReady = null;

  function loadDenoiser(ctx) {
    if (!ctx.audioWorklet) return Promise.resolve(false);
    if (!denoiseReady) {
      denoiseReady = ctx.audioWorklet
        .addModule('/static/js/denoise.js')
        .then(function () { return true; })
        .catch(function () { return false; });
    }
    return denoiseReady;
  }

  function shapeMic(raw) {
    const ctx = ensureCtx();
    if (!ctx || ctx.state !== 'running') {
      RTC.micGainNode = null;
      RTC.micGateNode = null;
      RTC.micDenoiseNode = null;
      RTC.micAnalyser = null;
      return raw;
    }
    try {
      const source = ctx.createMediaStreamSource(raw);
      const analyser = ctx.createAnalyser();
      analyser.fftSize = 512;
      const gate = ctx.createGain();    // шумовой порог
      const gain = ctx.createGain();    // громкость
      const dest = ctx.createMediaStreamDestination();
      dest.channelCount = 1;

      gate.gain.value = 1;
      gain.gain.value = RTC.micGain;

      source.connect(analyser);         // измеряем до ворот, иначе порог себя заблокирует
      source.connect(gate);
      gate.connect(gain);
      gain.connect(dest);

      RTC.micAnalyser = analyser;
      RTC.micGateNode = gate;
      RTC.micGainNode = gain;
      RTC.micDenoiseNode = null;

      // Шумодав встраивается между воротами и громкостью, когда модуль
      // догрузится: до этого момента звук уже идёт, просто без фильтра.
      loadDenoiser(ctx).then(function (ok) {
        if (!ok || RTC.micGateNode !== gate) return;
        try {
          const node = new AudioWorkletNode(ctx, 'denoiser', {
            numberOfInputs: 1,
            numberOfOutputs: 1,
            outputChannelCount: [1]
          });
          node.parameters.get('strength').value = RTC.micDenoise;
          gate.disconnect(gain);
          gate.connect(node);
          node.connect(gain);
          RTC.micDenoiseNode = node;
          emit('denoise_ready', {});
        } catch (err) { /* остаёмся без шумодава */ }
      });

      startGate();
      return dest.stream;
    } catch (err) {
      RTC.micGainNode = null;
      RTC.micGateNode = null;
      RTC.micDenoiseNode = null;
      RTC.micAnalyser = null;
      return raw;
    }
  }

  /* Интенсивность 0..1 из интерфейса: 0 — выключен, 1 — максимум. */
  RTC.setDenoise = function (value) {
    RTC.micDenoise = Math.max(0, Math.min(1, value)) * 2;
    if (RTC.micDenoiseNode) {
      RTC.micDenoiseNode.parameters.get('strength').value = RTC.micDenoise;
      return true;
    }
    return false;
  };

  RTC.getDenoise = function () { return RTC.micDenoise / 2; };

  RTC.canDenoise = function () { return !!RTC.micDenoiseNode; };

  /* Ворота: пока уровень ниже порога, микрофон не передаёт ничего.
     Частый цикл нужен именно здесь — на медленном шумоподавитель
     срезал бы начала слов. Удержание после спада не даёт обрубать хвосты. */
  let gateTimer = null;
  let gateOpen = true;
  let gateSpeaking = false;

  function stopGate() {
    if (gateTimer) { clearInterval(gateTimer); gateTimer = null; }
  }

  function startGate() {
    stopGate();
    const ctx = audioCtx;
    const analyser = RTC.micAnalyser;
    const gate = RTC.micGateNode;
    if (!ctx || !analyser || !gate) return;

    const buffer = new Uint8Array(analyser.frequencyBinCount);
    let openUntil = 0;
    gateOpen = true;

    gateTimer = setInterval(function () {
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) {
        const v = (buffer[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buffer.length);
      RTC.localLevel = rms;

      const now = Date.now();
      if (rms >= RTC.micThreshold) openUntil = now + 280;
      const shouldOpen = RTC.micThreshold <= 0 || now < openUntil;

      if (shouldOpen !== gateOpen) {
        gateOpen = shouldOpen;
        // плавно: открываем быстро, закрываем мягче, чтобы не щёлкало
        gate.gain.setTargetAtTime(shouldOpen ? 1 : 0, ctx.currentTime, shouldOpen ? 0.008 : 0.06);
      }

      const speaking = shouldOpen && RTC.micEnabled && rms > 0.012;
      if (speaking !== gateSpeaking) {
        gateSpeaking = speaking;
        emit('speaking', { sid: 'self', speaking: speaking });
      }
    }, 45);
  }

  /* Чувствительность 0..1: 1 — передавать всё, 0 — только громкую речь. */
  RTC.setMicSensitivity = function (value) {
    const clamped = Math.max(0, Math.min(1, value));
    RTC.micThreshold = (1 - clamped) * 0.15;
    return !!RTC.micGateNode;
  };

  RTC.getMicSensitivity = function () {
    return 1 - RTC.micThreshold / 0.15;
  };

  RTC.gateIsOpen = function () { return gateOpen; };

  RTC.setMicGain = function (value) {
    RTC.micGain = value;
    if (RTC.micGainNode) {
      RTC.micGainNode.gain.value = value;
      return true;
    }
    return false;   // регулировать нечем, громкость останется системной
  };

  RTC.canTuneMic = function () { return !!RTC.micGainNode; };

  RTC.raiseHand = function (up) {
    RTC.handUp = !!up;
    if (RTC.socket && RTC.channelId) RTC.socket.emit('raise_hand', { up: RTC.handUp });
    return RTC.handUp;
  };

  RTC.join = async function (channelId) {
    if (RTC.channelId === channelId) return { listenOnly: false, error: null };
    if (RTC.joining) throw new Error('Подключение уже выполняется. Подождите несколько секунд.');
    if (!RTC.socket.connected) throw new Error('Нет связи с сервером. Дождитесь восстановления соединения.');
    if (RTC.channelId) RTC.leave();

    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      const err = new Error('Браузер не даёт доступ к микрофону: нужен https:// или localhost.');
      err.name = 'SecurityError';
      throw err;
    }

    RTC.joining = true;
    const generation = ++joinGeneration;
    RTC.lastError = '';
    let failure = null;
    try {
      await RTC.resumeAudio();
      const response = await fetch('/api/rtc-config', { cache: 'no-store', signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error('Не удалось получить настройки голосовой связи. Обновите страницу.');
      const config = await response.json();
      ICE_CONFIG = { iceServers: config.iceServers, iceCandidatePoolSize: 0 };
      RTC.hasTurn = config.hasTurn;
      if (generation !== joinGeneration) throw new Error('Подключение отменено');
    try {
      const raw = await grabMic();
      if (generation !== joinGeneration) {
        raw.getTracks().forEach(function (track) { track.stop(); });
        throw new DOMException('Подключение отменено', 'AbortError');
      }
      RTC.rawMicStream = raw;
      RTC.micStream = shapeMic(raw);
    } catch (err) {
      // Нет устройства или оно занято — заходим на прослушивание.
      // Отказ в доступе и небезопасный контекст пробрасываем наверх.
      if (['NotAllowedError', 'SecurityError', 'AbortError'].includes(err.name)) throw err;
      RTC.rawMicStream = null;
      RTC.micStream = null;
      failure = err;
    }

    RTC.micEnabled = !!RTC.micStream;
    RTC.channelId = channelId;
    RTC.handUp = false;

    // при работающих воротах индикатор речи считает уже их цикл
    if (RTC.micStream && !RTC.micAnalyser) {
      RTC.stopLocalMeter = watchLevel('self', RTC.micStream);
    }

    await new Promise(function (resolve, reject) {
      RTC.socket.timeout(10000).emit('voice_join', { channel_id: channelId, muted: !RTC.micEnabled }, function (err, reply) {
        if (generation !== joinGeneration) { reject(new Error('Подключение отменено')); return; }
        if (err) { reject(new Error('Сервер не подтвердил вход в голосовой канал. Попробуйте ещё раз.')); return; }
        if (!reply || !reply.ok) { reject(new Error(reply && reply.error || 'Вход в канал не подтверждён')); return; }
        resolve();
      });
    });
    return { listenOnly: !RTC.micStream, error: failure };
    } catch (err) {
      if (generation === joinGeneration) RTC.leave();
      throw err;
    } finally {
      RTC.joining = false;
    }
  };

  /* Повторная попытка уже внутри канала: гарнитуру могли воткнуть позже.
     Дорожка подменяется на лету, пересогласование не нужно. */
  RTC.retryMic = async function () {
    if (!RTC.channelId) throw new Error('Сначала зайдите в голосовой канал');
    const generation = joinGeneration;
    const raw = await grabMic();
    if (generation !== joinGeneration || !RTC.channelId) {
      raw.getTracks().forEach(function (track) { track.stop(); });
      throw new Error('Вы уже вышли из голосового канала');
    }
    RTC.rawMicStream = raw;
    const stream = shapeMic(raw);
    RTC.micStream = stream;
    RTC.micEnabled = true;

    const track = stream.getAudioTracks()[0];
    for (const peer of RTC.peers.values()) {
      if (peer.micTx && peer.micTx.sender) {
        await peer.micTx.sender.replaceTrack(track);
      }
    }

    if (RTC.stopLocalMeter) { RTC.stopLocalMeter(); RTC.stopLocalMeter = null; }
    if (!RTC.micAnalyser) RTC.stopLocalMeter = watchLevel('self', stream);
    RTC.socket.emit('voice_state', { muted: false });
    return true;
  };

  RTC.leave = function () {
    joinGeneration += 1;
    earlyIce.clear();
    signalQueues.clear();
    RTC.peers.forEach(function (_peer, sid) { closePeer(sid); });
    RTC.peers.clear();

    if (RTC.stopLocalMeter) { RTC.stopLocalMeter(); RTC.stopLocalMeter = null; }
    [RTC.rawMicStream, RTC.micStream].forEach(function (stream) {
      if (stream) stream.getTracks().forEach(function (t) { t.stop(); });
    });
    stopGate();
    RTC.rawMicStream = null;
    RTC.micStream = null;
    RTC.micGainNode = null;
    RTC.micGateNode = null;
    RTC.micDenoiseNode = null;
    RTC.micAnalyser = null;
    RTC.localLevel = 0;
    RTC.handUp = false;
    RTC.silenced = {};
    RTC.stopShare(true);

    if (RTC.channelId && RTC.socket) RTC.socket.emit('voice_leave', {});
    RTC.channelId = null;
    RTC.selfSid = null;
    RTC.peerUsers = {};
    RTC.micEnabled = false;
    emit('playback', { blocked: false });
    emit('left', {});
  };

  RTC.setMicEnabled = function (on) {
    if (!RTC.micStream) return null;
    RTC.micEnabled = !!on;
    const streams = [RTC.rawMicStream, RTC.micStream];
    streams.forEach(function (stream) {
      if (stream) stream.getAudioTracks().forEach(function (t) { t.enabled = RTC.micEnabled; });
    });
    RTC.socket.emit('voice_state', { muted: !RTC.micEnabled });
    return RTC.micEnabled;
  };

  RTC.toggleMic = function () {
    if (!RTC.micStream) return null;   // микрофона нет — включать нечего
    return RTC.setMicEnabled(!RTC.micEnabled);
  };

  // ------------------------------------------------------------- экран

  RTC.startShare = async function () {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
      throw new Error('Браузер не умеет захватывать экран. Нужен localhost или HTTPS.');
    }
    const preset = QUALITY[RTC.quality];
    const video = {
      frameRate: { ideal: preset.fps, max: preset.fps },
      displaySurface: 'monitor'
    };
    if (preset.width) {
      video.width = { ideal: preset.width, max: preset.width };
      video.height = { ideal: preset.height, max: preset.height };
    }

    const wantAudio = RTC.screenAudioMode !== 'off';
    const request = {
      video: video,
      audio: wantAudio && {
        echoCancellation: false,
        noiseSuppression: false,
        autoGainControl: false
      },
      /* Свою же вкладку в списке выбора не показываем: её захват давал бы
         бесконечное зеркало и возврат голосов участников. */
      selfBrowserSurface: 'exclude',
      /* Системный звук содержит и то, что звучит у нас, то есть голоса
         остальных. Когда пользователь отказался от звука экрана, просим
         браузер даже не предлагать его. */
      systemAudio: wantAudio ? 'include' : 'exclude',
      surfaceSwitching: 'include'
    };

    const stream = await navigator.mediaDevices.getDisplayMedia(request);

    RTC.screenStream = stream;
    RTC.screenTrack = stream.getVideoTracks()[0];
    RTC.screenTrack.contentHint = preset.hint;

    const settings = RTC.screenTrack.getSettings ? RTC.screenTrack.getSettings() : {};
    RTC.screenSurface = settings.displaySurface || 'unknown';

    const captured = stream.getAudioTracks()[0] || null;
    RTC.screenAudioRaw = captured;
    RTC.screenAudioTrack = shapeScreenAudio(captured);

    RTC.screenTrack.onended = function () { RTC.stopShare(); };

    for (const peer of RTC.peers.values()) {
      if (peer.videoTx) {
        try { await peer.videoTx.sender.replaceTrack(RTC.screenTrack); } catch (err) { /* пропускаем */ }
      }
      if (peer.screenAudioTx && RTC.screenAudioTrack) {
        try { await peer.screenAudioTx.sender.replaceTrack(RTC.screenAudioTrack); } catch (err) { /* пропускаем */ }
      }
      applyQualityToPeer(peer);
    }

    RTC.socket.emit('voice_state', { sharing: true, quality: RTC.quality });
    emit('share_started', { stream: stream });
    return stream;
  };

  /* Захват вкладки берёт звук только этой вкладки, и нашей среди них нет —
     возвращать нечего. А вот захват всего экрана тянет весь системный вывод,
     включая голоса участников: их приглушаем, пока кто-то говорит. */
  function needsDucking() {
    return RTC.screenAudioMode === 'duck' && RTC.screenSurface !== 'browser';
  }

  function shapeScreenAudio(track) {
    stopDucking();
    if (!track || !needsDucking()) return track;

    const ctx = ensureCtx();
    const mix = referenceMix();
    if (!ctx || !mix || ctx.state !== 'running') return track;

    try {
      const source = ctx.createMediaStreamSource(new MediaStream([track]));
      /* Небольшая задержка даёт упреждение: детектор видит чужую речь
         раньше, чем соответствующий кусок звука уйдёт участникам, поэтому
         в эфир не проскакивает даже первый слог. */
      const delay = ctx.createDelay(0.5);
      delay.delayTime.value = 0.06;
      const gain = ctx.createGain();
      const dest = ctx.createMediaStreamDestination();

      source.connect(delay);
      delay.connect(gain);
      gain.connect(dest);

      RTC.duckNode = gain;
      startDucking();
      return dest.stream.getAudioTracks()[0] || track;
    } catch (err) {
      return track;
    }
  }

  let duckTimer = null;

  function stopDucking() {
    if (duckTimer) { clearInterval(duckTimer); duckTimer = null; }
    RTC.duckNode = null;
  }

  function startDucking() {
    const ctx = audioCtx;
    const analyser = RTC.refAnalyser;
    const gain = RTC.duckNode;
    if (!ctx || !analyser || !gain) return;

    const buffer = new Uint8Array(analyser.frequencyBinCount);
    let openUntil = 0;
    let ducking = false;

    duckTimer = setInterval(function () {
      analyser.getByteTimeDomainData(buffer);
      let sum = 0;
      for (let i = 0; i < buffer.length; i++) {
        const v = (buffer[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / buffer.length);

      const now = Date.now();
      if (rms > 0.015) openUntil = now + 300;   // держим после конца фразы
      const shouldDuck = now < openUntil;

      if (shouldDuck !== ducking) {
        ducking = shouldDuck;
        // вниз почти мгновенно, вверх мягко, чтобы не дёргало громкость
        gain.gain.setTargetAtTime(shouldDuck ? 0 : 1, ctx.currentTime,
                                  shouldDuck ? 0.005 : 0.08);
      }
    }, 20);
  }

  /* off — звук экрана не берём вовсе, duck — приглушаем во время чужой речи,
     raw — отдаём как есть. Меняется до начала показа. */
  RTC.setScreenAudioMode = function (mode) {
    RTC.screenAudioMode = ['off', 'duck', 'raw'].indexOf(mode) >= 0 ? mode : 'duck';
  };

  RTC.hasScreenAudio = function () { return !!RTC.screenAudioTrack; };

  RTC.stopShare = function (silent) {
    if (!RTC.screenStream) return;

    RTC.peers.forEach(function (peer) {
      if (peer.videoTx) peer.videoTx.sender.replaceTrack(null).catch(function () {});
      if (peer.screenAudioTx) peer.screenAudioTx.sender.replaceTrack(null).catch(function () {});
    });

    stopDucking();
    RTC.screenStream.getTracks().forEach(function (t) { t.stop(); });
    if (RTC.screenAudioTrack) RTC.screenAudioTrack.stop();
    if (RTC.screenAudioRaw) RTC.screenAudioRaw.stop();
    RTC.screenStream = null;
    RTC.screenTrack = null;
    RTC.screenAudioTrack = null;
    RTC.screenAudioRaw = null;
    RTC.screenSurface = null;

    if (!silent && RTC.socket) RTC.socket.emit('voice_state', { sharing: false });
    emit('share_stopped', {});
  };

  RTC.isSharing = function () { return !!RTC.screenStream; };

  // ------------------------------------------------------------- подписка на сокет

  RTC.attach = function (socket) {
    RTC.socket = socket;

    socket.on('voice_joined', function (data) {
      if (data.channel_id !== RTC.channelId) return;
      RTC.selfSid = data.self_sid;
      data.peers.forEach(function (peer) {
        RTC.peerUsers[peer.sid] = peer.user_id;
      });
      data.peers.forEach(function (peer) {
        offerTo(peer.sid).catch(function (err) {
          console.error('offer', err);
          RTC.lastError = 'Не удалось начать голосовое соединение: ' + err.message;
          emit('notice', { level: 'error', text: RTC.lastError });
        });
      });
      emit('joined', data);
    });

    socket.on('peer_joined', function (data) {
      if (data.channel_id !== RTC.channelId) return;
      RTC.peerUsers[data.peer.sid] = data.peer.user_id;
      audioBin().querySelectorAll('audio').forEach(function (el) {
        if (el.dataset.sid === data.peer.sid) el.dataset.user = String(data.peer.user_id);
      });
      refreshVolumes();
      emit('peer_joined', data);
    });

    socket.on('peer_left', function (data) {
      if (data.channel_id !== RTC.channelId) return;
      closePeer(data.sid);
      delete RTC.peerUsers[data.sid];
      emit('peer_left', data);
    });

    socket.on('signal', function (data) {
      if (!RTC.channelId || data.channel_id !== RTC.channelId) return;
      const generation = joinGeneration;
      const previous = signalQueues.get(data.from) || Promise.resolve();
      const next = previous.then(function () {
        if (generation !== joinGeneration) return;
        return handleSignal(data.from, data.payload);
      }).catch(function (err) {
        if (generation !== joinGeneration) return;
        console.error('signal', err);
        RTC.lastError = 'Ошибка голосового соединения: ' + err.message;
        emit('notice', { level: 'error', text: RTC.lastError });
      });
      signalQueues.set(data.from, next);
      next.then(function () { if (signalQueues.get(data.from) === next) signalQueues.delete(data.from); });
    });

    socket.on('disconnect', function () { if (RTC.channelId || RTC.joining) RTC.leave(); });
    ['pointerdown', 'keydown'].forEach(function (name) {
      document.addEventListener(name, function () {
        if (blockedAudio.size) RTC.resumeAudio().catch(function () {});
      });
    });

    window.addEventListener('beforeunload', function () {
      if (RTC.channelId) RTC.leave();
    });
  };

  RTC.sfx = SFX;
  global.RTC = RTC;
  global.SFX = SFX;
})(window);
