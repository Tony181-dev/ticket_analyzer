"""
Wraps Playwright to drive the M365 Copilot web chat UI (https://m365.cloud.microsoft/chat) in a persistent browser profile, either headless or headed. The Browser class handles opening/closing the browser context, detecting whether the user is authenticated, running an interactive login flow, locating the message
composer across page frames, selecting the required chat model, and sending prompts with attachments while reading back the assistant's response and conversation URL. It is used exclusively by worker.py.
"""
import os
import re
import shutil
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
URL = "https://m365.cloud.microsoft/chat"
PROFILE = Path.home() / ".cache" / "consult_with_copilot" / "browser_profile"
DEFAULT_MODEL = "GPT 5.6 Sol Think deeper"  # legacy; the app passes the chosen model
MODEL_SELECT_ATTEMPTS = 3
def launch_args():
    # Must stay identical to setup.py's launch_args() so the profile behaves the same way
    # during the interactive setup login as it does later during headless/headed automation.
    args = ["--disable-blink-features=AutomationControlled"]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # Chromium refuses to start as root unless the sandbox is disabled. Only do this
        # when actually running as root (e.g. Linux containers) instead of unconditionally
        # weakening the sandbox on every machine.
        args.append("--no-sandbox")
    return args
class Browser:
    def __init__(self):
        self.pw = self.ctx = self.page = None
        self.mode = None
    def close(self):
        ctx, pw = self.ctx, self.pw
        self.ctx = self.page = self.pw = None
        self.mode = None
        try:
            if ctx:
                ctx.close()
        except Exception:
            pass
        try:
            if pw:
                pw.stop()
        except Exception:
            pass
    def open(self, headless=True):
        # Reuse an existing context when it already runs in the requested mode
        # (headless OR headed). Reusing the headed context lets a single visible
        # window (e.g. the one that logged in) be reused for every chat instead of
        # closing and reopening -- and re-authenticating -- on each request.
        if self.ctx and self.mode == bool(headless):
            try:
                pages = [page for page in self.ctx.pages if not page.is_closed()]
                if pages:
                    self.page = pages[-1]
                    return self.page
            except Exception:
                pass
        self.close()
        PROFILE.mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        self.mode = bool(headless)
        self.ctx = self.pw.chromium.launch_persistent_context(
            str(PROFILE),
            headless=bool(headless),
            accept_downloads=True,
            viewport={"width": 1440, "height": 1000},
            args=launch_args(),
        )
        self.page = next(
            (page for page in self.ctx.pages if not page.is_closed()), None
        ) or self.ctx.new_page()
        return self.page
    def roots(self, page):
        try:
            return [page] + [frame for frame in page.frames if frame != page.main_frame]
        except Exception:
            return []
    def login_url(self, url):
        return any(
            value in (url or "").lower()
            for value in ("login", "signin", "login.microsoftonline")
        )
    def visible(self, root, selector):
        try:
            matches = root.locator(selector)
            for index in range(min(matches.count(), 30) - 1, -1, -1):
                item = matches.nth(index)
                if item.is_visible() and item.is_enabled():
                    return item
        except Exception:
            pass
        return None
    def composer(self, page):
        selectors = (
            "textarea[placeholder]",
            "textarea",
            "[data-lexical-editor='true'][contenteditable='true']",
            "div[contenteditable='true'][role='textbox']",
            "[contenteditable='true'][aria-label*='message' i]",
            "[contenteditable='true'][aria-label*='ask' i]",
            "div[contenteditable='true']",
        )
        for root in self.roots(page):
            for selector in selectors:
                item = self.visible(root, selector)
                if item:
                    return item
        return None
    def wait_composer(self, page, ms=35000):
        # Do not bail out the moment page.url briefly looks like a login URL: Microsoft
        # routinely bounces an already-authenticated page through login.microsoftonline.com
        # for a normal silent SSO token refresh before landing back on the chat page, so a
        # snapshot of the URL taken mid-redirect is not reliable proof of being logged out.
        deadline = time.time() + ms / 1000
        while time.time() < deadline:
            if page.is_closed():
                return None
            item = self.composer(page)
            if item:
                return item
            page.wait_for_timeout(500)
        return None
    def _probe(self, page, waits, navigation_timeout=60000):
        # Try to reach the chat composer over one or more (reload + wait) rounds.
        for index, wait_ms in enumerate(waits):
            try:
                page.goto(
                    URL,
                    wait_until="domcontentloaded",
                    timeout=navigation_timeout,
                )
            except Exception:
                pass
            if self.wait_composer(page, wait_ms) is not None:
                return True
            # Only treat this as "not logged in" if, after waiting for the composer to
            # show up, we are still parked on a login page on the final attempt. Otherwise
            # keep retrying (the SSO redirect chain may simply need longer to settle).
            if self.login_url(page.url) and index >= len(waits) - 1:
                return False
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
        return False
    def auth(self, quick=False):
        # Headless authentication probe used by the status badge and before chat.
        try:
            page = self.open(True)
            return self._probe(
                page,
                (8000,) if quick else (30000, 30000),
                15000 if quick else 60000,
            )
        except Exception:
            return False
    def auth_headed(self, quick=False):
        # Headed authentication probe. Some tenants (Conditional Access / device
        # compliance) refuse to render M365 Copilot in a headless browser even when the
        # saved login is perfectly valid. Close the verification window afterwards;
        # Chromium persists the authenticated profile on disk for the next chat.
        try:
            page = self.open(False)
            return self._probe(
                page,
                (12000,) if quick else (45000,),
                15000 if quick else 60000,
            )
        except Exception:
            return False
        finally:
            self.close()
    def login(self, seconds=300):
        page = self.open(False)
        try:
            page.goto(URL, wait_until="domcontentloaded", timeout=60000)
            deadline = time.time() + seconds
            while time.time() < deadline:
                if page.is_closed():
                    break
                if not self.login_url(page.url) and self.wait_composer(page, 3000):
                    return True
                page.wait_for_timeout(750)
            return False
        finally:
            self.close()
    def logout(self):
        self.close()
        shutil.rmtree(PROFILE, ignore_errors=True)
    def _click_first(self, page, selectors, timeout=3000):
        last_error = None
        for root in self.roots(page):
            for selector in selectors:
                try:
                    root.locator(selector).first.click(timeout=timeout)
                    return
                except Exception as error:
                    last_error = error
        if last_error:
            raise last_error
    def _trigger(self, page):
        selectors = (
            '[data-test-id="gptSubMenuModelTrigger-OpenAI"]',
            '[data-testid="gptSubMenuModelTrigger-OpenAI"]',
            '[role="menuitem"][aria-haspopup="menu"]:has-text("GPT")',
        )
        for root in self.roots(page):
            for selector in selectors:
                try:
                    match = root.locator(selector)
                    if match.count() and match.first.is_visible():
                        return match.first
                except Exception:
                    pass
        return None
    def _option(self, page, name=DEFAULT_MODEL, wait_ms=5000):
        """Visible menu row for model `name` (case-insensitive text match)."""
        name = " ".join((name or DEFAULT_MODEL).split())
        legacy = name.casefold() == DEFAULT_MODEL.casefold()
        deadline = time.time() + wait_ms / 1000
        while time.time() < deadline:
            for root in self.roots(page):
                if legacy:
                    try:
                        mark = root.locator('[data-testid="checkmark-Gpt_5_6_Reasoning"]').first
                        if mark.count():
                            row = mark.locator("xpath=ancestor::*[@role='menuitemradio'][1]").first
                            if row.count() and row.is_visible():
                                return row
                    except Exception:
                        pass
                for role in ("menuitemradio", "menuitem", "option"):
                    try:
                        matches = root.get_by_role(role, name=re.compile(re.escape(name), re.I))
                        for index in range(min(matches.count(), 10)):
                            item = matches.nth(index)
                            if item.is_visible() and (item.get_attribute("aria-haspopup") or "") != "menu":
                                return item
                    except Exception:
                        pass
                try:
                    matches = root.get_by_text(re.compile(r"^\s*" + re.escape(name) + r"\s*$", re.I))
                    for index in range(min(matches.count(), 10)):
                        item = matches.nth(index)
                        if item.is_visible():
                            row = item.locator("xpath=ancestor::*[@role='menuitemradio' or @role='menuitem' or @role='option'][1]").first
                            return row if row.count() else item
                except Exception:
                    pass
            page.wait_for_timeout(150)
        return None
    def _dismiss_model_menu(self, page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
    def _menu_open(self, page):
        if self._trigger(page):
            return True
        for root in self.roots(page):
            try:
                items = root.locator('[role="menuitemradio"]')
                for index in range(min(items.count(), 20)):
                    if items.nth(index).is_visible():
                        return True
            except Exception:
                pass
        return False
    def _open_model_menu(self, page):
        # The picker button label changes with the selected model, so a text selector is
        # unreliable. Success = a model menu (GPT submenu trigger or model radio items) is visible.
        if self._menu_open(page):
            return True
        named = (
            'button[aria-label="Model Selector"]',
            'button[aria-label*="model" i]',
            'button[aria-label*="Think" i]',
            'button:has-text("Think")',
            'button:has-text("GPT")',
        )
        generic = 'button[aria-haspopup="menu"]'
        for selector in (*named, generic):
            for root in self.roots(page):
                try:
                    matches = root.locator(selector)
                    count = min(matches.count(), 15)
                except Exception:
                    continue
                # The picker sits in the composer at the bottom, so try the last buttons first.
                for index in range(count - 1, -1, -1):
                    try:
                        button = matches.nth(index)
                        if not button.is_visible():
                            continue
                        button.click(timeout=1500)
                        page.wait_for_timeout(400)
                        if self._menu_open(page):
                            return True
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
                    except Exception:
                        pass
        return False
    def _select_model_once(self, page, name):
        if not self._open_model_menu(page):
            raise RuntimeError("Model picker not found")
        option = self._option(page, name, 1500)  # newer pickers list models at the top level
        if not option:
            trigger = self._trigger(page)
            if not trigger:
                raise RuntimeError(f"Model {name!r} not found in the picker")
            if (trigger.get_attribute("aria-expanded") or "").lower() != "true":
                try:
                    trigger.hover(timeout=1500)
                    page.wait_for_timeout(250)
                except Exception:
                    pass
                if (trigger.get_attribute("aria-expanded") or "").lower() != "true":
                    trigger.click(timeout=3000)
                    page.wait_for_timeout(350)
            option = self._option(page, name, 6000)
        if not option:
            raise RuntimeError(f"Model {name!r} not found in the picker (check the exact name shown in M365)")
        if (option.get_attribute("aria-checked") or "").lower() != "true":
            option.click(timeout=3000)
            page.wait_for_timeout(500)
        return name
    def select_model(self, page, name=DEFAULT_MODEL, attempts=MODEL_SELECT_ATTEMPTS):
        requested = " ".join((name or DEFAULT_MODEL).split())
        attempts = max(1, int(attempts))
        last_error = None
        for attempt in range(1, attempts + 1):
            try:
                selected = self._select_model_once(page, requested)
                self._dismiss_model_menu(page)
                return selected
            except Exception as error:
                last_error = error
                self._dismiss_model_menu(page)
                if attempt < attempts:
                    page.wait_for_timeout(500 * attempt)
        raise RuntimeError(f"Unable to select {requested} after {attempts} attempts: {last_error}")
    def file_input(self, page):
        for root in self.roots(page):
            try:
                matches = root.locator("input[type='file']")
                if matches.count():
                    return matches.last
            except Exception:
                pass
        return None
    def upload(self, page, file_path):
        file_input = self.file_input(page)
        if not file_input:
            raise RuntimeError("Attachment control not found")
        file_input.set_input_files(file_path)
        name = Path(file_path).name
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                found = page.get_by_text(name, exact=False).count()
                busy = page.locator("[aria-busy='true'],[role='progressbar']").count()
                if found and not busy:
                    return
            except Exception:
                pass
            page.wait_for_timeout(500)
        raise RuntimeError("Attachment upload did not complete: " + name)
    def _cards(self, page):
        output, seen = [], set()
        selectors = (
            "[data-message-author-role='assistant']",
            "[data-author='assistant']",
            "[data-testid*='assistant-message' i]",
            "[data-testid*='assistant-response' i]",
            "[role='article']",
        )
        for root in self.roots(page):
            for selector in selectors:
                try:
                    matches = root.locator(selector)
                    for index in range(min(matches.count(), 200)):
                        item = matches.nth(index)
                        if not item.is_visible():
                            continue
                        text = (item.inner_text() or "").strip()
                        if text and text not in seen and len(text) < 60000:
                            seen.add(text)
                            output.append(text)
                except Exception:
                    pass
        return output
    def busy(self, page):
        selector = (
            "button[aria-label*='Stop generating' i],"
            "button[aria-label*='Stop responding' i],"
            "[aria-busy='true']"
        )
        for root in self.roots(page):
            try:
                matches = root.locator(selector)
                for index in range(min(matches.count(), 20)):
                    if matches.nth(index).is_visible():
                        return True
            except Exception:
                pass
        return False
    def composer_text(self, box):
        try:
            tag = (box.evaluate("el => el.tagName") or "").lower()
        except Exception:
            tag = ""
        try:
            if tag == "textarea":
                return box.input_value() or ""
            return box.inner_text() or ""
        except Exception:
            return ""
    def click_send(self, page):
        selectors = (
            "button[type='submit'][aria-label='Send']",
            "button[type='submit'][aria-label*='Send' i]",
            "button[aria-label='Send']",
            "button[type='submit']",
        )
        for root in self.roots(page):
            for selector in selectors:
                try:
                    match = root.locator(selector)
                    if not match.count():
                        continue
                    button = match.first
                    if not button.is_visible() or button.is_disabled():
                        continue
                    button.click(timeout=2000)
                    return True
                except Exception:
                    continue
        return False
    def open_chat_page(self, url, headless):
        page = self.open(headless)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return page
        except Exception as error:
            message = str(error).lower()
            if "target page, context or browser has been closed" not in message:
                raise
            self.close()
            page = self.open(headless)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return page
    def chat(self, prompt, url=None, files=None, headless=True, model=DEFAULT_MODEL):
        target_url = url or URL
        page = self.open_chat_page(target_url, headless)
        try:
            # Wait for the composer before deciding this is a login problem: a transient
            # silent-SSO redirect through login.microsoftonline.com is normal for a page
            # that is actually still authenticated, so page.url right after the initial
            # goto is not trustworthy on its own.
            box = self.wait_composer(page)
            if not box:
                # Reload once and wait noticeably longer before declaring a login
                # problem. A cold headless start or a slow SSO redirect chain can take
                # much longer than the first wait window, and reporting LOGIN_REQUIRED
                # too early is exactly what caused spurious 503 errors.
                try:
                    page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    pass
                box = self.wait_composer(page, 45000)
            if not box:
                if self.login_url(page.url):
                    raise RuntimeError("LOGIN_REQUIRED")
                raise RuntimeError("Composer not found")
            chosen = self.select_model(page, model)
            for file_path in files or []:
                self.upload(page, file_path)
            before = self._cards(page)
            box = self.wait_composer(page)
            box.click()
            try:
                box.fill(prompt)
            except Exception:
                box.press("Control+A")
                box.press("Backspace")
                box.press_sequentially(prompt, delay=0)
            box.press("Enter")
            # Enter alone is not always registered by this composer (multi-line editors,
            # an attachment upload still settling, etc.), so verify the message actually
            # left the composer before assuming it was sent. Only fall back to a second
            # Enter and then the real Send button if that quick check fails.
            sent = False
            check_deadline = time.time() + 4
            while time.time() < check_deadline:
                if self.busy(page) or len(self._cards(page)) > len(before):
                    sent = True
                    break
                if not self.composer_text(box).strip():
                    sent = True
                    break
                page.wait_for_timeout(300)
            if not sent:
                try:
                    box.press("Enter")
                except Exception:
                    pass
                page.wait_for_timeout(600)
                sent = (
                    self.busy(page)
                    or len(self._cards(page)) > len(before)
                    or not self.composer_text(box).strip()
                )
            if not sent:
                self.click_send(page)
            deadline = time.time() + 240
            last = ""
            stable = 0
            seen_busy = False
            while time.time() < deadline:
                page.wait_for_timeout(1000)
                is_busy = self.busy(page)
                seen_busy = seen_busy or is_busy
                cards = self._cards(page)
                previous = before[-1] if before else ""
                current = (
                    cards[-1]
                    if cards and (len(cards) > len(before) or cards[-1] != previous)
                    else ""
                )
                if current:
                    stable = stable + 1 if current == last else 0
                    last = current
                    if stable >= (2 if seen_busy else 4) and not is_busy:
                        return current, page.url, chosen
            raise RuntimeError(
                "Response timeout: assistant answer card was not detected"
            )
        finally:
            # Keep the context alive (headless OR headed) so it can be reused by the next
            # request via open(). Previously the headed context was closed here, which
            # forced a fresh (re-authenticating) launch on every headed chat.
            pass
