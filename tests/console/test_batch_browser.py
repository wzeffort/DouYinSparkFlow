"""Offline browser contract: every URL is served by the isolated TestClient."""
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from sqlalchemy import select

from spark_console.db import session_scope
from spark_console.models import DouyinContactIdentity, SparkTaskRecipient
from tests.console import test_batch_web as fixture_module


def exercise_batch_ui(output_dir):
    fixture = fixture_module.BatchWebTests()
    fixture.setUp()
    try:
        account_id, _ = fixture.prepare()
        with session_scope(fixture.engine) as db:
            for i in range(5):
                db.add(DouyinContactIdentity(account_id=account_id, sec_uid=f'fixture-{i}', nickname=f'测试好友{i}'))
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
                errors = []
                requests = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def serve(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.netloc != 'testserver':
                        route.abort()
                        return
                    response = fixture.client.request(request.method, parsed.path + ('?' + parsed.query if parsed.query else ''),
                        content=request.post_data_buffer, headers={'content-type': request.headers.get('content-type', '')},
                        follow_redirects=True)
                    requests.append((request.method, parsed.path, response.status_code))
                    headers = {key: value for key, value in response.headers.items() if key not in {'content-length', 'content-encoding'}}
                    route.fulfill(status=response.status_code, headers=headers, body=response.content)
                page.route('**/*', serve)
                page.goto('https://testserver/tasks')
                page.locator('[data-friend-options] button').first.wait_for()
                page.locator('[data-add-filtered]').click()
                assert page.locator('.batch-recipient-card').count() == 5
                assert page.locator('[data-add-recipient]').is_disabled()
                messages = page.locator('.batch-recipient-card textarea')
                for i in range(5):
                    messages.nth(i).fill(f'给好友{i}的专属消息')
                page.locator('#task-send-time').fill('09:00')
                page.screenshot(path=str(output_dir / 'batch-desktop.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.wait_for_function('document.querySelector(".sidebar").getBoundingClientRect().right <= 0')
                assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
                page.screenshot(path=str(output_dir / 'batch-mobile.png'), full_page=True)
                with page.expect_navigation(wait_until='networkidle'), page.expect_response(lambda response: response.request.method == 'POST') as submitted:
                    page.get_by_role('button', name='创建并启用任务', exact=True).click()
                assert submitted.value.status == 200, page.locator('.form-error').all_text_contents()
                page.wait_for_url('https://testserver/tasks')
                page.wait_for_load_state('networkidle')
                assert page.locator('.task-card').count() == 1, (requests, page.locator('body').inner_text()[:800])
                with session_scope(fixture.engine) as db:
                    rows = db.scalars(select(SparkTaskRecipient).order_by(SparkTaskRecipient.position)).all()
                    assert [r.message_template for r in rows] == [f'给好友{i}的专属消息' for i in range(5)]
                assert not errors
            finally:
                browser.close()
    finally:
        fixture.tearDown()


def test_offline_browser_batch_create_and_mobile_layout(tmp_path):
    exercise_batch_ui(tmp_path)
