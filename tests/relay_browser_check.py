"""Local-only fallback regression: real sockets, blocked ICE, synthetic media."""
import json
import argparse
from pathlib import Path
import time
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"
parser = argparse.ArgumentParser()
parser.add_argument('--base', default='http://127.0.0.1:2080')
args = parser.parse_args()
BASE = args.base.rstrip('/')
if urlparse(BASE).hostname not in ('127.0.0.1', 'localhost') or urlparse(BASE).scheme not in ('http', 'https'):
    raise SystemExit('This test only allows localhost HTTP(S) servers.')
checks, errors, evidence = [], [], {}


def passed(name):
    checks.append(name)
    print("PASS:", name, flush=True)


def audio_received(page, peers=1):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        result = page.evaluate("RTC.diagnostics()")
        if len(result["peers"]) == peers and all(p["state"] == "connected" and p["packets"] > 20 and p["energy"] > 0.00001 for p in result["peers"]):
            return result
        page.wait_for_timeout(100)
    raise AssertionError("Audio not received: " + json.dumps(result))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ])

    def login(name, *, blocked=True, no_mic=False):
        context = browser.new_context(permissions=["microphone"], viewport={"width": 1280, "height": 900},
                                      ignore_https_errors=BASE.startswith('https://'))
        if blocked:
            context.add_init_script("""const NativePeer = window.RTCPeerConnection;
              window.RTCPeerConnection = class extends NativePeer {
                constructor(config) { super({...config, iceServers: [], iceTransportPolicy: 'relay'}); }
              };""")
        if no_mic:
            context.add_init_script("""window.addEventListener('DOMContentLoaded', () => {
              window.originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
              navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('No test device', 'NotFoundError'); };
            });""")
        page = context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(BASE + "/login")
        page.locator('[name="username"]').fill(name)
        page.locator('[name="password"]').fill("local-test-only")
        page.locator('button[type="submit"]').first.click()
        page.wait_for_function("window.RTC && RTC.socket && RTC.socket.connected")
        return page

    try:
        alice, boris = login("alice"), login("boris")
        for page in (alice, boris):
            page.locator('[data-channel="3"]').click()
        evidence["two_way"] = [audio_received(page) for page in (alice, boris)]
        for result in evidence["two_way"]:
            assert result["peers"][0]["route"] == "server"
            assert result["transport"] == 'websocket'
        for page in (alice, boris):
            assert page.evaluate("[...RTC.peers.values()].every(p => p.pc.connectionState === 'closed' && p.relay.connected)")
            expect(page.locator("#voice-status-text")).to_contain_text("через сервер")
            # Inspect the actual output MediaStream, not just received packet counters.
            page.evaluate("""() => {
              const el = document.querySelector('audio[data-kind="mic"]');
              const ctx = new AudioContext(); const analyser = ctx.createAnalyser();
              ctx.createMediaStreamSource(el.srcObject).connect(analyser);
              window.outputProbe = {ctx, analyser};
            }""")
            page.wait_for_function("""() => {
              const data = new Float32Array(outputProbe.analyser.fftSize);
              outputProbe.analyser.getFloatTimeDomainData(data);
              return data.some(v => Math.abs(v) > 0.001);
            }""", timeout=10000)
        passed("automatic two-way audio fallback with all RTCPeerConnections closed; real output energy")

        alice.locator("#btn-connection").click()
        expect(alice.locator("#connection-details")).to_contain_text("через сервер")
        alice.screenshot(path=str(OUT / "relay-diagnostics.png"), animations="disabled")
        alice.keyboard.press("Escape")
        alice.locator("#input").fill("Chat during server audio " + str(time.time()))
        text = alice.locator("#input").input_value()
        alice.locator("#btn-send").click()
        expect(boris.locator('.msg__text').filter(has_text=text)).to_be_visible()
        passed("diagnostics and text chat while server audio is active")

        alice.locator("#btn-mic").click()
        boris.wait_for_timeout(700)
        before = boris.evaluate("RTC.diagnostics()")["peers"][0]["packets"]
        boris.wait_for_timeout(700)
        assert boris.evaluate("RTC.diagnostics()")["peers"][0]["packets"] == before
        alice.locator("#btn-mic").click()
        boris.wait_for_timeout(250)
        assert boris.evaluate("RTC.diagnostics()")["peers"][0]["packets"] > before
        passed("mute stops media delivery; unmute resumes without reconnecting")

        alice.evaluate("""() => {
          RTC.setScreenAudioMode('raw');
          navigator.mediaDevices.getDisplayMedia = async () => {
            const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 540;
            const ctx = canvas.getContext('2d'); let frame = 0;
            const draw = () => {ctx.fillStyle = '#154559'; ctx.fillRect(0, 0, 960, 540);
              ctx.fillStyle = '#ffb020'; ctx.font = '45px Arial'; ctx.fillText('SERVER RELAY', 50, 200);
              ctx.fillRect(50 + (frame++ % 600), 300, 80, 30);};
            draw(); window.screenTimer = setInterval(draw, 50);
            const stream = canvas.captureStream(20);
            const ac = new AudioContext(); const osc = ac.createOscillator();
            const gain = ac.createGain(); gain.gain.value = 0.04;
            const dest = ac.createMediaStreamDestination();
            osc.connect(gain); gain.connect(dest); osc.start();
            window.screenAudioContext = ac;
            stream.addTrack(dest.stream.getAudioTracks()[0]); return stream;
          };
        }""")
        alice.locator("#btn-share").click()
        boris.wait_for_function("[...RTC.peers.values()][0].relay.frames >= 4", timeout=15000)
        boris.wait_for_function("""() => {const v = document.querySelector('#broadcast video');
          return v && v.videoWidth > 0 && v.readyState >= 2 && !v.paused;}""", timeout=10000)
        pixel = boris.evaluate("""() => {const v = document.querySelector('#broadcast video');
          const c = document.createElement('canvas'); c.width = c.height = 1;
          const ctx = c.getContext('2d'); ctx.drawImage(v, 0, 0, 1, 1);
          return [...ctx.getImageData(0,0,1,1).data];}""")
        assert sum(pixel[:3]) > 70, pixel
        assert boris.evaluate("!!document.querySelector('audio[data-kind=screen]')")
        evidence["screen"] = {"pixel": pixel, "diagnostics": boris.evaluate("RTC.diagnostics()")}
        boris.screenshot(path=str(OUT / "relay-screen.png"), animations="disabled")
        alice.locator("#btn-quality").click()
        expect(alice.locator("#relay-quality-note")).to_be_visible()
        alice.locator('[data-quality="saver"]').click()
        alice.keyboard.press("Escape")
        alice.locator("#btn-share").click()
        boris.wait_for_function("!document.querySelector('#broadcast video')")
        assert boris.evaluate("!document.querySelector('audio[data-kind=screen]')")
        passed("screen images and screen audio, quality limitation displayed, stop releases playback")

        vera = login("vera", no_mic=True)
        vera.locator('[data-channel="3"]').click()
        evidence["listen_only"] = audio_received(vera, 2)
        assert not vera.evaluate("RTC.hasMic()")
        vera.evaluate("() => { navigator.mediaDevices.getUserMedia = window.originalGetUserMedia; }")
        vera.evaluate("RTC.retryMic()")
        evidence["three_way"] = [audio_received(page, 2) for page in (alice, boris, vera)]
        passed("three-way fallback and attaching a microphone after listen-only join")

        vera.locator("#btn-hangup").click()
        assert vera.evaluate("!RTC.channelId && !RTC.peers.size && !RTC.rawMicStream && !document.querySelector('audio[data-sid]')")
        boris.locator('[data-channel="4"]').click()
        alice.wait_for_function("RTC.peers.size === 0")
        assert alice.evaluate("!document.querySelector('audio[data-sid]')")
        boris.locator('[data-channel="3"]').click()
        evidence["rejoin"] = [audio_received(page) for page in (alice, boris)]
        passed("channel switching, hangup cleanup and rejoin with a fresh fallback session")
        # A slow/suspended browser cannot accumulate an unlimited server queue.
        boris.evaluate("""() => {
          const emit = RTC.socket.emit.bind(RTC.socket); const held = [];
          window.releaseRelayAcks = () => {RTC.socket.emit = emit; held.forEach(args => emit(...args));};
          RTC.socket.emit = function (...args) {
            if (args[0] === 'relay_ack') {held.push(args); return this;}
            return emit(...args);
          };
        }""")
        boris.wait_for_timeout(2200)
        stalled = boris.evaluate("RTC.diagnostics()")["peers"][0]["packets"]
        boris.wait_for_timeout(500)
        assert boris.evaluate("RTC.diagnostics()")["peers"][0]["packets"] == stalled
        boris.evaluate("window.releaseRelayAcks()")
        boris.wait_for_function("n => [...RTC.peers.values()][0].relay.packets > n + 8", arg=stalled)
        passed("stalled receiver resumes from a bounded queue")
        # Recreate the audio elements to exercise the common autoplay recovery.
        for page in (alice, boris):
            page.locator("#btn-hangup").click()
        alice.evaluate("""() => {
          window.allowRelayAudio = false;
          const play = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function () {
            if (this.tagName === 'AUDIO' && !window.allowRelayAudio) return Promise.reject(new DOMException('Blocked by test', 'NotAllowedError'));
            return play.call(this);
          };
        }""")
        for page in (alice, boris):
            page.locator('[data-channel="3"]').click()
        audio_received(alice)
        expect(alice.locator('#audio-banner')).to_be_visible()
        alice.evaluate('window.allowRelayAudio = true')
        alice.locator('#btn-enable-audio').click()
        expect(alice.locator('#audio-banner')).not_to_be_visible()
        assert alice.evaluate("[...document.querySelectorAll('audio[data-sid]')].every(el => !el.paused)")
        passed("explicit playback resumes server audio after autoplay denial")
        for page in (alice, boris):
            page.locator("#btn-hangup").click()

        # Mix a native WebRTC pair with a third browser whose ICE is blocked.
        for page in (alice, boris, vera):
            page.context.close()
        alice, boris, vera = login("alice", blocked=False), login("boris", blocked=False), login("vera")
        for page in (alice, boris):
            page.locator('[data-channel="3"]').click()
            page.wait_for_function("RTC.channelId === 3 && !RTC.joining")
        for page in (alice, boris):
            page.wait_for_function("[...RTC.peers.values()].some(p => p.pc.connectionState === 'connected')")
        vera.locator('[data-channel="3"]').click()
        evidence["mixed_routes"] = [audio_received(page, 2) for page in (alice, boris, vera)]
        for result in evidence["mixed_routes"][:2]:
            assert sum(peer["route"] == "server" for peer in result["peers"]) == 1
        assert all(peer["route"] == "server" for peer in evidence["mixed_routes"][2]["peers"])
        for page in (alice, boris, vera):
            page.locator("#btn-hangup").click()
        passed("direct WebRTC and server fallback coexist in the same three-person channel")
        assert not errors, errors
        passed("no JavaScript exceptions or console errors")
    finally:
        OUT.mkdir(exist_ok=True)
        name = 'relay-https-results.json' if BASE.startswith('https://') else 'relay-results.json'
        (OUT / name).write_text(json.dumps({"base": BASE, "checks": checks, "errors": errors, "evidence": evidence}, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
