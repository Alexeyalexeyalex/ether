"""Real browser/WebRTC checks, synthetic media, localhost only.

Start tests/local_server.py first. Uses installed Edge, no personal browser profile.
"""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts"
OUT.mkdir(exist_ok=True)
BASE = "http://127.0.0.1:2080"
checks = []
errors = []
evidence = {}


def passed(name):
    checks.append(name)
    print("PASS:", name, flush=True)


def joined(page, peers):
    page.wait_for_function("""count => !RTC.joining && RTC.peers.size === count &&
      [...RTC.peers.values()].every(p => p.pc.connectionState === 'connected')""", arg=peers, timeout=25000)


def audio_received(page, peers):
    # Poll resolved getStats results in Python: wait_for_function treats a Promise
    # as truthy in some driver versions and can falsely pass before media arrives.
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        result = page.evaluate("RTC.diagnostics()")
        if len(result["peers"]) == peers and all(peer["packets"] > 20 and peer["energy"] > 0.00001 for peer in result["peers"]):
            return result
        page.wait_for_timeout(150)
    raise AssertionError("Audio not received: " + json.dumps(result))


with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ])

    def login(name, *, no_mic=False):
        context = browser.new_context(permissions=["microphone"], viewport={"width": 1440, "height": 1000})
        if no_mic:
            context.add_init_script("""window.addEventListener('DOMContentLoaded', () => {
                window.originalGetUserMedia = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                navigator.mediaDevices.getUserMedia = async () => { throw new DOMException('No test device', 'NotFoundError'); };
            });""")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
        page.goto(BASE + "/login")
        page.locator('[name="username"]').fill(name)
        page.locator('[name="password"]').fill("local-test-only")
        page.locator('button[type="submit"]').first.click()
        page.wait_for_function("window.RTC && RTC.socket.connected")
        expect(page.locator('[data-channel="3"]')).to_be_visible()
        return page

    try:
        alice = login("alice")
        boris = login("boris")
        passed("login, room and channel loading, independent user sessions")
        alice.screenshot(path=str(OUT / "after-empty-desktop.png"))

        # Send through the mobile-friendly button; receive and persist through real Socket.IO.
        message = "Проверка связи: встречаемся в общем эфире. " + str(int(time.time()))
        alice.locator("#input").fill(message)
        alice.locator("#btn-send").click()
        expect(boris.locator(".msg__text").filter(has_text=message)).to_be_visible()
        boris.reload()
        expect(boris.locator(".msg__text").filter(has_text=message)).to_be_visible()
        passed("chat send, delivery and persisted history after reload")

        alice.locator('[data-channel="3"]').click()
        alice.wait_for_function("RTC.channelId === 3 && !RTC.joining")
        # Deliberately deliver ICE before the SDP offer, reproducing network reordering.
        boris.evaluate("""() => {
          const emit = RTC.socket.emit.bind(RTC.socket);
          RTC.socket.emit = function (event, data, ...args) {
            if (event === 'signal' && data.payload.type === 'offer') {
              setTimeout(() => emit(event, data, ...args), 700); return this;
            }
            return emit(event, data, ...args);
          };
        }""")
        boris.locator('[data-channel="3"]').click()
        for page in [alice, boris]:
            joined(page, 1)
        evidence["two_way_audio"] = [audio_received(page, 1) for page in [alice, boris]]
        passed("two-way WebRTC audio with ICE arriving before offer (real RTP and nonzero audio energy)")

        alice.locator("#btn-mic").click()
        alice.wait_for_function("!RTC.micEnabled && !RTC.rawMicStream.getAudioTracks()[0].enabled")
        expect(boris.locator('.occupant[data-user="2"] .occupant__tag--muted')).to_be_visible()
        alice.locator("#btn-mic").click()
        alice.wait_for_function("RTC.micEnabled && RTC.rawMicStream.getAudioTracks()[0].enabled")
        alice.locator("#btn-hand").click()
        expect(boris.locator('.occupant[data-user="2"]')).to_have_class("occupant is-hand")
        alice.locator("#btn-hand").click()
        passed("microphone toggle and raised-hand state delivered to the other participant")

        alice.locator("#btn-connection").click()
        expect(alice.locator("#connection-details")).to_contain_text("Связь установлена")
        expect(alice.locator("#connection-details")).to_contain_text("Получено аудиопакетов")
        alice.screenshot(path=str(OUT / "after-diagnostics.png"))
        alice.keyboard.press("Escape")
        expect(alice.locator("#btn-connection")).to_be_focused()
        passed("connection diagnostics and keyboard focus restoration")

        # Synthetic screen source: test transmission and decoding, no desktop capture.
        alice.evaluate("""() => {
          navigator.mediaDevices.getDisplayMedia = async () => {
            const canvas = document.createElement('canvas'); canvas.width = 960; canvas.height = 540;
            const ctx = canvas.getContext('2d'); let frame = 0;
            const draw = () => { ctx.fillStyle = '#122d37'; ctx.fillRect(0, 0, 960, 540);
              ctx.fillStyle = '#ffb020'; ctx.font = '48px Arial'; ctx.fillText('ЭФИР · LOCAL SCREEN', 70, 240);
              ctx.fillStyle = '#ffffff'; ctx.fillRect(70 + (frame++ % 700), 300, 50, 8); };
            draw(); window.testScreenTimer = setInterval(draw, 50);
            return canvas.captureStream(20);
          };
        }""")
        alice.locator("#btn-share").click()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            frames = boris.evaluate("""async () => {
              for (const p of RTC.peers.values()) for (const s of (await p.pc.getStats()).values()) {
                if (s.type === 'inbound-rtp' && s.kind === 'video') return s.framesDecoded || 0;
              } return 0;
            }""")
            if frames > 5:
                break
            boris.wait_for_timeout(150)
        assert frames > 5
        evidence["screen_frames_decoded"] = frames
        expect(boris.locator("#broadcast video")).to_be_visible()
        boris.wait_for_function("""() => {
          const v = document.querySelector('#broadcast video');
          return v && v.videoWidth > 0 && v.readyState >= 2 && !v.paused;
        }""", timeout=10000)
        evidence["screen_pixel"] = boris.evaluate("""() => {
          const v = document.querySelector('#broadcast video');
          const c = document.createElement('canvas'); c.width = c.height = 1;
          const ctx = c.getContext('2d'); ctx.drawImage(v, 0, 0, 1, 1);
          return [...ctx.getImageData(0, 0, 1, 1).data];
        }""")
        assert sum(evidence["screen_pixel"][:3]) > 30, evidence["screen_pixel"]
        boris.screenshot(path=str(OUT / "after-screen-share.png"), animations="disabled")
        alice.locator("#btn-quality").click()
        alice.locator('[data-quality="standard"]').click()
        alice.keyboard.press("Escape")
        alice.locator("#btn-share").click()
        expect(boris.locator("#broadcast")).not_to_be_visible()
        alice.evaluate("clearInterval(window.testScreenTimer)")
        passed("screen sharing, received decoded video frames, quality switch and stop")

        vera = login("vera", no_mic=True)
        vera.locator('[data-channel="3"]').click()
        for page in [alice, boris, vera]:
            joined(page, 2)
        evidence["listen_only"] = audio_received(vera, 2)
        expect(vera.locator("#btn-mic")).to_contain_text("Нет микрофона")
        vera.evaluate("() => { navigator.mediaDevices.getUserMedia = window.originalGetUserMedia; }")
        vera.locator("#btn-mic").click()
        vera.wait_for_function("RTC.hasMic() && RTC.micEnabled")
        evidence["three_way_audio"] = [audio_received(page, 2) for page in [alice, boris, vera]]
        passed("three participants, listen-only mode and microphone attachment without rejoining")

        vera.locator("#btn-hangup").click()
        vera.wait_for_function("!RTC.channelId && RTC.peers.size === 0 && !RTC.rawMicStream")
        for page in [alice, boris]:
            joined(page, 1)
        boris.locator('[data-channel="4"]').click()
        boris.wait_for_function("RTC.channelId === 4 && !RTC.joining")
        alice.wait_for_function("RTC.peers.size === 0")
        boris.locator('[data-channel="3"]').click()
        for page in [alice, boris]:
            joined(page, 1)
            audio_received(page, 1)
        passed("hangup cleanup, switching channels and two-way audio after rejoin")

        # Loss of signaling keeps the unsent message and requires an explicit new voice join.
        boris.locator("#input").fill("Черновик не должен пропасть")
        boris.evaluate("RTC.socket.disconnect()")
        expect(boris.locator("#connection-banner")).to_be_visible()
        expect(boris.locator("#btn-send")).to_be_disabled()
        expect(boris.locator("#input")).to_have_value("Черновик не должен пропасть")
        boris.evaluate("RTC.socket.connect()")
        boris.wait_for_function("RTC.socket.connected")
        expect(boris.locator("#connection-banner")).not_to_be_visible()
        expect(boris.locator("#input")).to_have_value("Черновик не должен пропасть")
        message2 = "Связь восстановлена. " + str(int(time.time()))
        alice.locator("#input").fill(message2)
        alice.locator("#btn-send").click()
        expect(boris.locator(".msg__text").filter(has_text=message2)).to_be_visible()
        passed("signaling reconnect, channel resubscription and retained draft")

        # Force HTTP polling on a complete app session, as with a proxy that cannot upgrade.
        boris.wait_for_function("RTC.socket.io.engine.transport.name === 'websocket'")
        boris.evaluate("RTC.socket.disconnect(); RTC.socket.io.opts.transports = ['polling']; RTC.socket.io.opts.upgrade = false; RTC.socket.connect()")
        boris.wait_for_function("RTC.socket.connected && RTC.socket.io.engine.transport.name === 'polling'")
        boris.locator('[data-channel="3"]').click()
        for page in [alice, boris]:
            joined(page, 1)
            audio_received(page, 1)
        passed("voice and signaling using HTTP polling without WebSocket")

        alice.screenshot(path=str(OUT / "after-desktop.png"))
        for width in [390, 320, 768]:
            alice.set_viewport_size({"width": width, "height": 844})
            assert alice.evaluate("document.documentElement.scrollWidth <= innerWidth"), f"Overflow at {width}px"
            expect(alice.locator("#btn-send")).to_be_visible()
            expect(alice.locator("#btn-hangup")).to_be_visible()
            alice.screenshot(path=str(OUT / f"after-mobile-{width}.png"), animations="disabled")
            alice.locator("#btn-menu").click()
            expect(alice.locator("#room-list")).to_be_visible()
            alice.screenshot(path=str(OUT / f"after-navigation-{width}.png"), animations="disabled")
            alice.locator("#btn-close-nav").click()
        passed("mobile layouts and navigation at 320, 390 and 768px without horizontal overflow")

        # Refusal and cancellation must release any captured microphone and not show a false join.
        vera.evaluate("""() => { navigator.mediaDevices.getUserMedia = async () => {
          throw new DOMException('Permission denied in test', 'NotAllowedError'); }; }""")
        denied = vera.evaluate("async () => { try { await RTC.join(3); return null; } catch (e) { return e.name; } }")
        assert denied == "NotAllowedError"
        assert vera.evaluate("!RTC.channelId && !RTC.rawMicStream && !RTC.joining")
        vera.evaluate("() => { navigator.mediaDevices.getUserMedia = window.originalGetUserMedia; }")
        rejected = vera.evaluate("async () => { try { await RTC.join(9999); return false; } catch (e) { return !RTC.channelId && !RTC.rawMicStream; } }")
        assert rejected
        vera.evaluate("""() => {
          navigator.mediaDevices.getUserMedia = async (...args) => {
            const stream = await window.originalGetUserMedia(...args); window.cancelledStream = stream;
            await new Promise(r => setTimeout(r, 300)); return stream;
          };
          window.pendingJoin = RTC.join(3).catch(e => e.name);
        }""")
        vera.wait_for_function("window.cancelledStream")
        vera.evaluate("RTC.leave()")
        vera.evaluate("async () => { await window.pendingJoin; }")
        assert vera.evaluate("!RTC.channelId && window.cancelledStream.getTracks().every(t => t.readyState === 'ended')")
        passed("microphone permission denied, server-rejected join and cancellation release the microphone")

        alice.set_viewport_size({"width": 1440, "height": 1000})
        for page in [alice, boris]:
            page.locator("#btn-hangup").click()
        alice.evaluate("""() => {
          window.blockAudio = true;
          window.originalPlay = HTMLMediaElement.prototype.play;
          HTMLMediaElement.prototype.play = function () {
            if (this.tagName === 'AUDIO' && window.blockAudio) return Promise.reject(new DOMException('Autoplay blocked in test', 'NotAllowedError'));
            return window.originalPlay.call(this);
          };
        }""")
        # Both requests are started without waiting for the other's response.
        alice.evaluate("() => { window.simultaneousJoin = RTC.join(3); }")
        boris.evaluate("() => { window.simultaneousJoin = RTC.join(3); }")
        for page in [alice, boris]:
            joined(page, 1)
            audio_received(page, 1)
        expect(alice.locator("#audio-banner")).to_be_visible()
        alice.evaluate("window.blockAudio = false")
        alice.locator("#btn-enable-audio").click()
        expect(alice.locator("#audio-banner")).not_to_be_visible()
        assert alice.evaluate("[...document.querySelectorAll('audio')].every(a => !a.paused)")
        passed("simultaneous voice joins and recovery from browser autoplay blocking")

        offerer, answerer = (alice, boris) if alice.evaluate("[...RTC.peers.values()][0].isOfferer") else (boris, alice)
        old_sdp = offerer.evaluate("[...RTC.peers.values()][0].pc.localDescription.sdp")
        answerer.evaluate("""() => { RTC.socket.emit('signal', {
          to: [...RTC.peers.keys()][0], channel_id: RTC.channelId, payload: {type: 'restart'}
        }); }""")
        offerer.wait_for_function("old => [...RTC.peers.values()][0].pc.localDescription.sdp !== old", arg=old_sdp)
        for page in [alice, boris]:
            joined(page, 1)
            audio_received(page, 1)
        passed("ICE restart renegotiation restores a stable two-way audio connection")

        assert not errors, errors
        passed("no JavaScript exceptions or browser console errors")
    finally:
        (OUT / "browser-results.json").write_text(json.dumps({"checks": checks, "errors": errors, "evidence": evidence}, ensure_ascii=False, indent=2), encoding="utf-8")
        browser.close()
