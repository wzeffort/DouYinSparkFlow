"""Real DOM regression for QR/SMS entry; all HTTP is served by a local TestClient."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect

from tests.console import test_account_scan_web as scan_fixture


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('mobile', [False, True])
def test_scan_button_opens_dialog_and_creates_session_in_browser(mobile, failed):
    executable = os.environ.get('SPARK_TEST_BROWSER_EXECUTABLE')
    if not executable:
        pytest.skip('set SPARK_TEST_BROWSER_EXECUTABLE to a local Chromium browser')
    assert Path(executable).is_file()
    fixture = scan_fixture.AccountScanWebTests()
    fixture.setUp()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(executable_path=executable, headless=True)
            try:
                page = browser.new_page(viewport={'width': 390 if mobile else 1280, 'height': 844})
                errors, starts = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def serve(route):
                    request = route.request
                    if request.method == 'POST' and request.url.endswith('/accounts/scan'):
                        starts.append(request.post_data)
                    if failed and request.method == 'GET' and '/accounts/scan/' in request.url:
                        route.fulfill(json={'id': 'fixture', 'status': 'failed', 'remaining_seconds': 0,
                            'message': 'automation failed', 'login_mode': 'sms' if mobile else 'qr'})
                        return
                    response = fixture.owner_client.request(request.method, request.url,
                        content=request.post_data_buffer,
                        headers={'content-type': request.headers.get('content-type', '')})
                    route.fulfill(status=response.status_code,
                        headers={k: v for k, v in response.headers.items()
                                 if k.lower() not in {'content-length', 'content-encoding', 'transfer-encoding'}},
                        body=response.content)
                page.route('**/*', serve)
                page.goto('https://testserver/accounts')
                page.locator('#scan-start').click()
                expect(page.locator("#scan-dialog")).to_be_visible()
                if mobile:
                    assert "手机号" in page.locator("#scan-dialog-title").inner_text()
                if failed:
                    expect(page.locator("#scan-status")).to_have_text("automation failed")
                    assert page.locator('#scan-placeholder').is_hidden()
                    assert page.locator('.scan-frame').is_hidden()
                    assert not page.locator('#scan-start').is_disabled()
                    if mobile:
                        assert '手机号' in page.locator('#scan-start').inner_text()
                assert not errors, errors
                assert len(starts) == 1
                assert ('mode=sms' if mobile else 'mode=qr') in starts[0]
                if not failed:
                    page.locator('#scan-cancel').click()
                    expect(page.locator('#scan-dialog')).not_to_be_visible()
                    page.locator('#scan-start').click()
                    expect(page.locator('#scan-dialog')).to_be_visible()
                    assert len(starts) == 2
                    assert ('mode=sms' if mobile else 'mode=qr') in starts[1]

            finally:
                browser.close()
    finally:
        fixture.tearDown()
