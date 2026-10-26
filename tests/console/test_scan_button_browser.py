"""Real DOM regression for QR/SMS entry; all HTTP is served by a local TestClient."""
import os
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from tests.console import test_account_scan_web as scan_fixture


@pytest.mark.parametrize('mobile', [False, True])
def test_scan_button_opens_dialog_and_creates_session_in_browser(mobile):
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
                page.wait_for_function("document.querySelector('#scan-dialog').open")
                assert not errors, errors
                assert len(starts) == 1
                assert ('mode=sms' if mobile else 'mode=qr') in starts[0]
            finally:
                browser.close()
    finally:
        fixture.tearDown()
