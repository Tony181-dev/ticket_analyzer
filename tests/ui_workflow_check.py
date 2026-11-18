"""Browser smoke check with a local Jira attachment fixture; no AI or external services.

Run: python tests/ui_workflow_check.py (requires Playwright Chromium).
"""
import io
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["TA_DATA_DIR"] = tempfile.mkdtemp(prefix="ta-ui-check-")
os.environ["TA_AXEL"] = ""

from app import attachments, events, paths, pipeline, server, settings
from playwright.sync_api import sync_playwright, expect


def zipped(name, content):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(name, content)
    return stream.getvalue()


DATA = zipped("inner.zip", zipped("main.log", "12:30:00 E Connection failed\n"))


class FixtureHandler(server.Handler):
    def do_GET(self):
        if self.path == "/fixture/logs.zip":
            return self._send(200, DATA, "application/zip")
        return super().do_GET()


def main():
    thread_errors = []
    previous_hook = threading.excepthook
    threading.excepthook = lambda args: thread_errors.append(str(args.exc_value))
    paths.ensure_dirs()
    settings.save({"assistant_proactive": False, "lang": "vi", "ai_mode": "copilot"})
    httpd = server.Server(("127.0.0.1", 0), FixtureHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    run = pipeline.create("UI-1", download_dir=str(paths.DATA / "downloads"))
    run.update(summary="Bluetooth disconnects after pairing", attachments=[{
        "id": "1", "filename": "logs.zip", "size": len(DATA), "kind": "archive", "url": base + "/fixture/logs.zip"}])
    run["steps"]["jira"]["status"] = "done"
    run["steps"]["understand"]["status"] = "done"
    run['steps']['understand']['elapsed_seconds'] = 37
    run['usage_passes'] = [{'n': 1, 'input_tokens': 100, 'output_tokens': 50, 'model_calls': 1}]
    pipeline._write(run, "01-jira.md", "Bluetooth disconnects after pairing")
    pipeline._write(run, "02-understanding.md", "# Hiểu sơ bộ ticket\n\nKết nối bị ngắt sau khi ghép đôi. Chưa kiểm tra log.")
    analysis = pipeline._write(run, '05-analysis.md', 'Old analysis to be cleared by reset')
    pipeline._save(run)
    try:
        with mock.patch.object(server.doctor, "checks", return_value={"checks": []}), \
                mock.patch.object(server.mcp, "status", return_value={}), \
                mock.patch.object(attachments, "_jira_auth", return_value=("", {})), sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel=os.environ.get("TA_TEST_BROWSER_CHANNEL") or "chrome")
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(base + "/?run=" + run["id"])
            expect(page.locator('#runList .run-item.active')).to_be_visible()
            expect(page.locator('#m365HeadlessOption')).to_be_hidden()
            page.locator('.mode-btn[data-mode=m365]').click()
            expect(page.locator('#m365HeadlessOption')).to_be_visible()
            page.locator('#m365Headless').uncheck()
            page.wait_for_function('!document.querySelector("#m365Headless").disabled')
            assert settings.load()['m365_headless'] is False
            page.locator('#settingsBtn').click()
            expect(page.locator('[name=m365_headless]')).not_to_be_checked()
            page.locator('#settingsModal [data-close]').click()
            page.locator('#m365Headless').check()
            page.wait_for_function('!document.querySelector("#m365Headless").disabled')
            assert settings.load()['m365_headless'] is True
            page.screenshot(path=str(paths.DATA / 'm365-controls.png'))
            events.publish('busy', run_id=run['id'], busy=True)
            running = page.locator(f'#runList .run-item[data-id="{run["id"]}"]')
            expect(running).to_have_class('run-item active running')
            expect(running.locator('.history-running-tag')).to_contain_text('Đang chạy')
            expect(running.locator('.history-running-tag svg use')).to_have_attribute('href', '#i-gear')
            expect(page.locator('#m365Headless')).to_be_disabled()
            expect(page.locator('#resetWorkflow')).to_be_disabled()
            page.locator('#newRun').click()
            expect(running).to_have_class('run-item  running')
            expect(running.locator('.history-running-tag')).to_be_visible()
            page.screenshot(path=str(paths.DATA / 'history-running.png'))
            events.publish('busy', run_id=run['id'], busy=False)
            expect(running.locator('.history-running-tag')).to_have_count(0)
            running.locator('.history-open').click()
            page.locator('.mode-btn[data-mode=copilot]').click()
            expect(page.locator('#m365HeadlessOption')).to_be_hidden()
            page.locator('#settingsBtn').click()
            expect(page.locator('.m365-settings [name=m365_model]')).to_be_visible()
            expect(page.locator('.m365-settings [name=m365_headless]')).to_be_visible()
            page.locator('#s_cli').focus()
            expect(page.locator('#s_cliMenu')).to_be_visible()
            page.locator('#s_cliMenu [data-value="claude-sonnet-5.5"]').click()
            expect(page.locator('#s_cli')).to_have_value('claude-sonnet-5.5')
            page.locator('#settingsForm [data-help="cfg.budgetHelp"]').click()
            expect(page.locator('#helpText')).to_contain_text('2.000')
            page.locator('#helpModal [data-close]').first.click()
            expect(page.locator('#settingsModal')).to_be_visible()
            expect(page.locator('#s_cli')).to_have_value('claude-sonnet-5.5')
            page.screenshot(path=str(paths.DATA / 'settings.png'))
            page.locator('#settingsForm button[type=submit]').click()
            expect(page.locator('#settingsModal')).to_be_hidden()
            assert settings.load()['copilot_model'] == 'claude-sonnet-5.5'
            page.locator('[data-help="gate.help"]').click()
            expect(page.locator('#helpText li')).to_have_count(3)
            page.locator('#helpModal [data-close]').first.click()
            page.locator('#attProgBtn').click()
            expect(page.locator('#ticketAttachmentsTab')).to_have_attribute('aria-selected', 'true')
            expect(page.locator('#attList')).to_contain_text('logs.zip')
            expect(page.locator('#attRun')).to_be_enabled()
            page.locator('#attRun').click()
            expect(page.locator('#attProgActive')).to_be_hidden(timeout=10000)
            expect(page.locator('#attProgModal')).to_be_visible()
            expect(page.locator('#attRun')).to_be_enabled(timeout=10000)
            page.locator('#extractedFilesTab').click()
            inner = page.locator('#extractedFiles .scanned-file').filter(has_text='inner.zip').first
            expect(inner.locator('[data-extract-path]')).to_be_visible()
            inner.locator('[data-extract-path]').click()
            expect(page.locator('#extractedFiles')).to_contain_text('main.log', timeout=10000)
            expect(page.locator('#attProgActive')).to_be_hidden(timeout=10000)
            expect(page.locator('#attRun')).to_be_enabled(timeout=10000)
            assert [Path(f).name for f in run['inputs']['log_files']] == ['main.log']
            page.screenshot(path=str(paths.DATA / 'attachments.png'))
            page.locator('#attProgModal [data-close]').click()
            page.locator('#resetWorkflowControl [data-help]').click()
            expect(page.locator('#helpModal')).to_be_visible()
            page.locator('#helpModal [data-close]').first.click()
            page.locator('#resetWorkflow').click()
            page.screenshot(path=str(paths.DATA / 'reset-confirmation.png'))
            page.locator('#resetWorkflowModal [data-close]').click()
            assert analysis.exists() and run['usage_total']['total_tokens'] > 0
            previous_session = run['session_id']
            page.locator('#resetWorkflow').click()
            page.locator('#confirmResetWorkflow').click()
            expect(page.locator('#resetWorkflowModal')).to_be_hidden()
            assert not analysis.exists()
            assert run['usage_total']['total_tokens'] == 0
            assert all(state['elapsed_seconds'] == 0 for state in run['steps'].values())
            assert run['session_id'] != previous_session
            assert (paths.DATA / 'downloads' / 'UI-1' / 'logs.zip').exists()
            page.locator('#runList .run-item').hover()
            expect(page.locator('.history-delete')).to_have_css('opacity', '1')
            page.locator('.history-delete').click()
            page.locator('#deleteRunModal [data-close]').click()
            expect(page.locator('#runList .run-item')).to_have_count(1)
            page.locator('.history-delete').click()
            page.locator('#confirmDeleteRun').click()
            expect(page.locator('#runList .run-item')).to_have_count(0)
            assert (paths.DATA / 'downloads' / 'UI-1' / 'logs.zip').exists()
            page.locator('#settingsBtn').click()
            page.set_viewport_size({"width": 640, "height": 900})
            assert page.locator('#settingsModal .modal-box').evaluate('(el) => el.scrollWidth <= el.clientWidth')
            page.screenshot(path=str(paths.DATA / 'settings-narrow.png'))
            assert errors == [], errors
            assert thread_errors == [], thread_errors
            browser.close()
        print(f"Browser checks passed. Screenshots: {paths.DATA}")
    finally:
        threading.excepthook = previous_hook
        httpd.shutdown()
        httpd.server_close()


if __name__ == '__main__':
    main()
