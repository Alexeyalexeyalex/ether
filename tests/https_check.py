"""Smoke test of main.py --local --port 2081 --ssl with an isolated instance."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.launch(channel="msedge", headless=True, args=[
        "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
    ])
    # This exception applies only to the self-signed localhost test certificate.
    context = browser.new_context(ignore_https_errors=True, permissions=["microphone"])
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda err: errors.append(str(err)))
    page.goto("https://127.0.0.1:2081/login")
    page.locator('[name="username"]').fill("admin")
    page.locator('[name="password"]').fill("123Alex12")
    page.locator('button[type="submit"]').first.click()
    page.wait_for_function("window.RTC && RTC.socket && RTC.socket.connected")
    page.locator('[data-channel="3"]').click()
    page.wait_for_function("RTC.channelId === 3 && !RTC.joining && RTC.hasMic()")
    page.wait_for_function("RTC.socket.io.engine.transport.name === 'websocket'")
    result = page.evaluate("RTC.diagnostics()")
    assert result["secure"] and result["signaling"] and result["microphone"]
    response = context.request.get("https://127.0.0.1:2081/admin/")
    assert response.status == 200
    result["admin_status"] = response.status
    result["errors"] = errors
    assert not errors
    output = Path(__file__).resolve().parents[1] / "artifacts/https-results.json"
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result))
    browser.close()
