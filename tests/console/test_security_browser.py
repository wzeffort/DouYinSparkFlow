"""All browser requests are fulfilled locally; never contact Douyin or production."""
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from tests.console import test_registration_web as fixtures


def test_csp_preserves_admin_scripts_filters_and_delete_confirmation():
    case = fixtures.RegistrationWebTests()
    case.setUp()
    try:
        case.login()
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                writes = []

                def serve(route):
                    request = route.request
                    url = urlsplit(request.url)
                    if url.netloc != 'testserver':
                        route.abort()
                        return
                    if request.method == 'POST':
                        writes.append(url.path)
                    response = case.client.request(request.method, url.path + ('?' + url.query if url.query else ''),
                        content=request.post_data_buffer, headers={'content-type': request.headers.get('content-type', '')},
                        follow_redirects=True)
                    body = response.content
                    if url.path == '/admin':
                        body = body.replace(b'</body>', b'<script>window.unsafeInjected=true</script></body>')
                    headers = {k: v for k, v in response.headers.items() if k not in {'content-length', 'content-encoding'}}
                    route.fulfill(status=response.status_code, headers=headers, body=body)

                page.route('**/*', serve)
                page.goto('https://testserver/admin')
                page.wait_for_load_state('networkidle')
                assert page.evaluate('typeof showToast') == 'function'
                assert page.evaluate('window.unsafeInjected === undefined')
                dialogs = []
                def reject(dialog):
                    dialogs.append(dialog.message)
                    dialog.dismiss()
                page.on('dialog', reject)
                page.locator('form[data-confirm] button').first.click()
                assert dialogs
                assert writes == []
                page.locator('#invite-status').select_option('active')
                page.wait_for_url('**invite_status=active*')
                assert page.locator('.invite-card').count() == 1
            finally:
                browser.close()
    finally:
        case.tearDown()
