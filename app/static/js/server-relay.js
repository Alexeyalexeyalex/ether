/* Media fallback over the authenticated site socket, with bounded queues.
 * Audio: PCM16, mono, 24 kHz. Screen: independent JPEG, up to 1280x720 / 5 fps.
 * WebRTC remains the preferred transport; this module serves failed pairs.
 */
(function (global) {
  'use strict';
  const RATE = 24000;

  function encode(bytes) {
    let result = '';
    for (let i = 0; i < bytes.length; i += 8192) result += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    return btoa(result);
  }

  function decode(text) {
    const raw = atob(text);
    const bytes = new Uint8Array(raw.length);
    for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
    return bytes;
  }

  class ServerRelay {
    constructor(hooks) {
      this.hooks = hooks;
      this.peers = new Map();
      this.sources = new Map();
      this.module = null;
      this.sequence = { mic: 0, screen_audio: 0, screen: 0 };
      this.screenSource = null;
      this.screenTimer = null;
      hooks.socket.on('relay_media', data => this.receive(data));
    }

    async prepare() {
      const ctx = this.hooks.context();
      if (!ctx || !ctx.audioWorklet || !global.AudioWorkletNode) {
        throw new Error('Для резервного звука обновите браузер и откройте сайт по HTTPS.');
      }
      if (!this.module) this.module = ctx.audioWorklet.addModule('/static/js/relay-capture.js').catch(err => {
        this.module = null;
        throw err;
      });
      await this.module;
    }

    add(sid, token) {
      const existing = this.peers.get(sid);
      if (existing && existing.token === token) return existing;
      this.remove(sid);
      const peer = { sid: sid, token: token, connected: false, packets: 0, energy: 0,
        frames: 0, outputs: new Map(), last: {}, screen: null, screenVersion: 0, sharing: false };
      this.peers.set(sid, peer);
      return peer;
    }

    connect(sid, token) {
      const peer = this.peers.get(sid);
      if (!peer || peer.token !== token) return;
      peer.connected = true;
      this.refresh();
    }

    writable(kind) {
      const socket = this.hooks.socket;
      const engine = socket.io.engine;
      if (!socket.connected || !engine || !engine.transport || !engine.transport.writable) return false;
      if (engine.writeBuffer.length > 1) return false;
      const ws = engine.transport.ws;
      return !ws || ws.bufferedAmount < (kind === 'screen' ? 8000 : 32000);
    }

    publish(kind, data) {
      if (!this.hooks.channel() || !this.hooks.epoch() || !this.writable(kind)) return;
      if (![...this.peers.values()].some(peer => peer.connected)) return;
      this.hooks.socket.volatile.emit('relay_publish', { kind: kind, data: data,
        seq: this.sequence[kind]++, channel_id: this.hooks.channel(), epoch: this.hooks.epoch() });
    }

    refresh() {
      const active = [...this.peers.values()].some(peer => peer.connected);
      const desired = active ? this.hooks.sources() : {};
      for (const kind of ['mic', 'screen_audio']) {
        const track = desired[kind];
        const source = this.sources.get(kind);
        if (source && source.track !== track) this.stopSource(kind);
        if (!track || track.readyState !== 'live' || this.sources.has(kind)) continue;
        const ctx = this.hooks.context();
        try {
          const input = ctx.createMediaStreamSource(new MediaStream([track]));
          const node = new AudioWorkletNode(ctx, 'efir-relay-capture');
          const silent = ctx.createGain();
          silent.gain.value = 0;
          const entry = { track: track, input: input, node: node, silent: silent };
          this.sources.set(kind, entry);
          node.port.onmessage = event => {
            if (this.sources.get(kind) !== entry || ctx.currentTime - event.data.time > 0.3) return;
            if (kind === 'mic' && !this.hooks.micEnabled()) return;
            this.publish(kind, encode(new Uint8Array(event.data.buffer)));
          };
          input.connect(node);
          node.connect(silent);
          silent.connect(ctx.destination);
        } catch (err) { this.hooks.error('Не удалось передать звук через сервер: ' + err.message); }
      }
      const screen = desired.screen;
      if (this.screenSource && this.screenSource.track !== screen) this.stopScreenSource();
      if (screen && !this.screenSource) this.startScreenSource(screen);
    }

    stopSource(kind) {
      const source = this.sources.get(kind);
      if (!source) return;
      source.node.port.onmessage = null;
      source.node.port.close();
      source.input.disconnect();
      source.node.disconnect();
      source.silent.disconnect();
      this.sources.delete(kind);
    }

    startScreenSource(track) {
      const video = document.createElement('video');
      video.muted = true;
      video.playsInline = true;
      video.srcObject = new MediaStream([track]);
      const source = { track: track, video: video, canvas: document.createElement('canvas'), busy: false };
      this.screenSource = source;
      video.play().catch(() => {});
      this.screenTimer = setInterval(async () => {
        if (this.screenSource !== source || source.busy || video.readyState < 2 || !this.writable('screen')) return;
        source.busy = true;
        try {
          const small = this.hooks.quality() === 'saver';
          const scale = Math.min(1, (small ? 960 : 1280) / video.videoWidth, (small ? 540 : 720) / video.videoHeight);
          const canvas = source.canvas;
          canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
          canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
          const ctx = canvas.getContext('2d', { alpha: false });
          ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
          let blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.65));
          for (let attempt = 0; blob && blob.size > 128 * 1024 && attempt < 3; attempt++) {
            canvas.width = Math.max(1, Math.round(canvas.width * 0.75));
            canvas.height = Math.max(1, Math.round(canvas.height * 0.75));
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            blob = await new Promise(resolve => canvas.toBlob(resolve, 'image/jpeg', 0.45));
          }
          if (!blob || blob.size > 128 * 1024 || this.screenSource !== source) return;
          const bytes = new Uint8Array(await blob.arrayBuffer());
          if (this.screenSource === source) this.publish('screen', encode(bytes));
        } catch (err) {
          if (this.screenSource === source) this.hooks.error('Не удалось передать изображение через сервер. Остановите и включите показ экрана снова.');
        } finally { source.busy = false; }
      }, 200);
    }

    stopScreenSource() {
      clearInterval(this.screenTimer);
      this.screenTimer = null;
      if (this.screenSource) {
        this.screenSource.video.pause();
        this.screenSource.video.srcObject = null;
        this.screenSource = null;
      }
    }

    output(peer, kind) {
      let output = peer.outputs.get(kind);
      if (!output) {
        const ctx = this.hooks.context();
        const destination = ctx.createMediaStreamDestination();
        destination.channelCount = 1;
        output = { destination: destination, next: 0, playing: new Set() };
        peer.outputs.set(kind, output);
        this.hooks.audio(peer.sid, kind === 'mic' ? 'mic' : 'screen', destination.stream.getAudioTracks()[0]);
      }
      return output;
    }

    audio(peer, kind, packet) {
      const bytes = decode(packet.data);
      if (bytes.length !== 1920) return;
      const ctx = this.hooks.context();
      const output = this.output(peer, kind);
      // Bounded latency: do not schedule an ever-growing backlog after a stall.
      if (ctx.state !== 'running' || output.next > ctx.currentTime + 0.4) return;
      const buffer = ctx.createBuffer(1, 960, RATE);
      const samples = buffer.getChannelData(0);
      const view = new DataView(bytes.buffer);
      let energy = 0;
      for (let i = 0; i < samples.length; i++) {
        samples[i] = view.getInt16(i * 2, true) / 32768;
        energy += samples[i] * samples[i];
      }
      const node = ctx.createBufferSource();
      node.buffer = buffer;
      node.connect(output.destination);
      const at = output.next < ctx.currentTime ? ctx.currentTime + 0.12 : output.next;
      output.next = at + buffer.duration;
      output.playing.add(node);
      node.onended = () => { output.playing.delete(node); node.disconnect(); };
      node.start(at);
      peer.packets++;
      peer.energy += energy / RATE;
    }

    async image(peer, packet) {
      if (!peer.sharing) return;
      const version = peer.screenVersion;
      const bytes = decode(packet.data);
      const blob = new Blob([bytes], { type: 'image/jpeg' });
      const url = URL.createObjectURL(blob);
      const img = new Image();
      try {
        await new Promise((resolve, reject) => { img.onload = resolve; img.onerror = reject; img.src = url; });
        if (this.peers.get(peer.sid) !== peer || peer.screenVersion !== version || !peer.sharing) return;
        if (!img.naturalWidth || img.naturalWidth > 1280 || img.naturalHeight > 720) return;
        if (!peer.screen) {
          const canvas = document.createElement('canvas');
          canvas.width = img.naturalWidth;
          canvas.height = img.naturalHeight;
          if (!canvas.captureStream) throw new Error('Браузер не поддерживает резервный показ экрана.');
          const stream = canvas.captureStream(5);
          peer.screen = { canvas: canvas, stream: stream };
          this.hooks.screen(peer.sid, stream);
        }
        const canvas = peer.screen.canvas;
        if (canvas.width !== img.naturalWidth) canvas.width = img.naturalWidth;
        if (canvas.height !== img.naturalHeight) canvas.height = img.naturalHeight;
        canvas.getContext('2d', { alpha: false }).drawImage(img, 0, 0);
        const track = peer.screen.stream.getVideoTracks()[0];
        if (track.requestFrame) track.requestFrame();
        peer.frames++;
      } finally { img.src = ''; URL.revokeObjectURL(url); }
    }

    async receive(data) {
      if (!data || data.channel_id !== this.hooks.channel()) return;
      const peer = this.peers.get(data.from);
      if (!peer || peer.token !== data.token || !Array.isArray(data.packets)) return;
      try {
        for (const packet of data.packets) {
          if (this.peers.get(peer.sid) !== peer || (peer.last[data.kind] != null && packet.seq <= peer.last[data.kind])) continue;
          peer.last[data.kind] = packet.seq;
          if (data.kind === 'screen') await this.image(peer, packet);
          else if (data.kind === 'mic' || (data.kind === 'screen_audio' && peer.sharing)) this.audio(peer, data.kind, packet);
        }
      } catch (err) { this.hooks.error('Не удалось воспроизвести медиа через сервер. Обновите браузер.'); }
      finally {
        if (this.hooks.channel() === data.channel_id) this.hooks.socket.emit('relay_ack', { from: data.from, kind: data.kind, id: data.id });
      }
    }

    sharing(sid, on) {
      const peer = this.peers.get(sid);
      if (!peer) return;
      peer.sharing = !!on;
      if (!on) this.clearScreen(peer);
    }

    clearScreen(peer) {
      peer.screenVersion++;
      if (peer.screen) {
        peer.screen.stream.getTracks().forEach(track => track.stop());
        peer.screen = null;
        this.hooks.screenOff(peer.sid);
      }
      this.stopOutput(peer, 'screen_audio');
    }

    stopOutput(peer, kind) {
      const output = peer.outputs.get(kind);
      if (!output) return;
      output.playing.forEach(node => { try { node.stop(); node.disconnect(); } catch (_) {} });
      output.destination.stream.getTracks().forEach(track => track.stop());
      output.destination.disconnect();
      peer.outputs.delete(kind);
      this.hooks.audioOff(peer.sid, kind === 'mic' ? 'mic' : 'screen');
    }

    remove(sid) {
      const peer = this.peers.get(sid);
      if (!peer) return;
      this.peers.delete(sid);
      this.clearScreen(peer);
      this.stopOutput(peer, 'mic');
      if (!this.peers.size) {
        [...this.sources.keys()].forEach(kind => this.stopSource(kind));
        this.stopScreenSource();
      }
    }

    reset() {
      [...this.peers.keys()].forEach(sid => this.remove(sid));
      [...this.sources.keys()].forEach(kind => this.stopSource(kind));
      this.stopScreenSource();
    }
  }
  global.ServerRelay = ServerRelay;
})(window);
