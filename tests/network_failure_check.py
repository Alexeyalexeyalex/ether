"""No available ICE route: failures must be visible, retry bounded, cleanup complete.

All sessions and requests use the disposable localhost:2080 test server.
"""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
    ])
    pages = []
    errors = []
    try:
        for name in ["alice", "boris"]:
            context = browser.new_context(permissions=["microphone"])
            # Use a real RTCPeerConnection with no allowed route, not a mock state.
            context.add_init_script("""const NativePeer = window.RTCPeerConnection;
              window.RTCPeerConnection = class extends NativePeer {
                constructor(config) { super({...config, iceServers: [], iceTransportPolicy: 'relay'}); }
              };""")
            page = context.new_page()
            page.on("pageerror", lambda err: errors.append(str(err)))
            page.goto("http://127.0.0.1:2080/login")
            page.locator('[name="username"]').fill(name)
            page.locator('[name="password"]').fill("local-test-only")
            page.locator('button[type="submit"]').first.click()
            page.locator('[data-channel="3"]').click()
            pages.append(page)
        pages[0].wait_for_function("RTC.connectionSummary().failed", timeout=55000)
        results = []
        for page in pages:
            page.wait_for_function("RTC.connectionSummary().failed", timeout=15000)
            expect(page.locator("#voice-status-text")).to_contain_text("Связь не установлена")
            result = page.evaluate("RTC.diagnostics()")
            assert "TURN" in result["lastError"]
            assert result["peers"][0]["packets"] == 0
            assert page.evaluate("[...RTC.peers.values()].every(p => p.restarts === 2 && !p.recoveryTimer)")
            results.append(result)
        pages[0].locator("#btn-connection").click()
        expect(pages[0].locator("#connection-details")).to_contain_text("TURN")
        expect(pages[0].locator("#connection-details")).to_contain_text("Нет связи")
        pages[0].screenshot(path=str(ROOT / "artifacts/after-network-failure.png"), animations="disabled")
        pages[0].locator("#btn-reconnect").click()
        assert not pages[0].evaluate("RTC.connectionSummary().failed")
        pages[0].keyboard.press("Escape")
        for page in pages:
            page.locator("#btn-hangup").click()
            assert page.evaluate("!RTC.channelId && !RTC.peers.size && !RTC.rawMicStream")
        assert not errors, errors
        (ROOT / "artifacts/network-failure-results.json").write_text(json.dumps({
            "passed": True, "errors": errors, "participants": results,
            "checks": ["no ICE route", "bounded retries", "visible TURN explanation", "manual retry", "hangup cleanup"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print("PASS: unavailable route, bounded retries, visible diagnostics, manual retry and cleanup")
    finally:
        browser.close()
