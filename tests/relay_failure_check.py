"""Local fallback preparation failure, visible error and manual recovery."""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
errors = []
with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True, args=[
        '--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream',
        '--autoplay-policy=no-user-gesture-required',
    ])
    pages = []
    try:
        for name in ('alice', 'boris'):
            context = browser.new_context(permissions=['microphone'])
            context.add_init_script('''const NativePeer = window.RTCPeerConnection;
              window.RTCPeerConnection = class extends NativePeer {
                constructor(config) { super({...config, iceServers: [], iceTransportPolicy: 'relay'}); }
              };''')
            page = context.new_page()
            page.on('pageerror', lambda err: errors.append(str(err)))
            page.goto('http://127.0.0.1:2080/login')
            page.locator('[name="username"]').fill(name)
            page.locator('[name="password"]').fill('local-test-only')
            page.locator('button[type="submit"]').first.click()
            page.wait_for_function('window.RTC && RTC.socket && RTC.socket.connected')
            pages.append(page)
        alice, boris = pages
        alice.evaluate('''async () => {
          const ctx = new AudioContext(); const proto = Object.getPrototypeOf(ctx.audioWorklet);
          const original = proto.addModule;
          window.restoreWorklet = () => {proto.addModule = original;};
          proto.addModule = function (url, ...args) {
            if (url.includes('relay-capture')) return Promise.reject(new Error('Test worklet loading failure'));
            return original.call(this, url, ...args);
          };
          await ctx.close();
        }''')
        for page in pages:
            page.locator('[data-channel="3"]').click()
        for page in pages:
            page.wait_for_function('RTC.connectionSummary().failed', timeout=35000)
            assert not page.evaluate('RTC.connectionSummary().connected')
            expect(page.locator('#voice-status-text')).to_contain_text('Связь не установлена')
        failed = [page.evaluate('RTC.diagnostics()') for page in pages]
        alice.evaluate('window.restoreWorklet()')
        alice.locator('#btn-connection').click()
        alice.locator('#btn-reconnect').click()
        alice.keyboard.press('Escape')
        restored = []
        for page in pages:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                result = page.evaluate('RTC.diagnostics()')
                if result['peers'][0]['packets'] > 10 and result['peers'][0]['energy'] > 0.00001:
                    break
                page.wait_for_timeout(100)
            assert result['peers'][0]['state'] == 'connected' and result['peers'][0]['packets'] > 10, result
            assert not result['lastError']
            restored.append(result)
        alice.set_viewport_size({'width': 390, 'height': 844})
        assert alice.evaluate('document.documentElement.scrollWidth <= innerWidth')
        alice.screenshot(path=str(ROOT / 'artifacts/relay-mobile.png'), animations='disabled')
        # Cancelling capture while the picker is pending must release its tracks.
        alice.evaluate('''() => {
          navigator.mediaDevices.getDisplayMedia = async () => {
            const canvas = document.createElement('canvas'); canvas.width = canvas.height = 10;
            const stream = canvas.captureStream(5); window.pendingScreen = stream;
            await new Promise(resolve => setTimeout(resolve, 300)); return stream;
          };
          window.shareResult = RTC.startShare().catch(err => err.name);
        }''')
        alice.wait_for_function('window.pendingScreen')
        alice.locator('#btn-hangup').click()
        assert alice.evaluate('async () => await window.shareResult') == 'AbortError'
        assert alice.evaluate('!RTC.screenStream && window.pendingScreen.getTracks().every(track => track.readyState === "ended")')
        boris.locator('#btn-hangup').click()
        assert not errors, errors
        (ROOT / 'artifacts/relay-failure-results.json').write_text(json.dumps({
            'passed': True, 'errors': errors, 'failed': failed, 'restored': restored,
            'checks': ['bounded preparation failure', 'visible error on both peers', 'manual retry restores audio', 'pending screen capture cancellation'],
        }, ensure_ascii=False, indent=2), encoding='utf-8')
        print('PASS: preparation failure, visible errors, manual recovery, pending screen capture cleanup')
    finally:
        browser.close()
