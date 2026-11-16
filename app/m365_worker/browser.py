"""
Wraps Playwright to drive the M365 Copilot web chat UI (https://m365.cloud.microsoft/chat) in a persistent browser profile, either headless or headed. The Browser class handles opening/closing the browser context, detecting whether the user is authenticated, running an interactive login flow, locating the message
composer across page frames, selecting the required chat model, and sending prompts with attachments while reading back the assistant's response and conversation URL. It is used exclusively by worker.py.
"""
import os
import base64
import re
import shutil
import time
from pathlib import Path
from playwright.sync_api import sync_playwright
URL = "https://m365.cloud.microsoft/chat"
PROFILE = Path.home() / ".cache" / "consult_with_copilot" / "browser_profile"
DEFAULT_MODEL = "GPT-5.6 Sol Think Deeper"  # legacy; the app passes the chosen model
MODEL_SELECT_ATTEMPTS = 3
# M365 Copilot will not send attachments on their own: an empty composer keeps Send disabled.
DEFAULT_PROMPT = "Thực hiện phân tích theo các file đính kèm. / Please analyze the attached files."
def _norm(text):
    return re.sub(r"[^a-z0-9]+", "", (text or "").casefold())
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
        self.images = []
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
                self.page = self._pick_page()
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
        self.page = self._pick_page()
        return self.page
    def _pick_page(self):
        # Drive exactly one tab: the M365 chat tab if present. Restored/popup tabs are closed
        # so the worker never types into (or leaves focus on) a stray page.
        pages = [page for page in self.ctx.pages if not page.is_closed()]
        if not pages:
            return self.ctx.new_page()
        hosts = ("m365.cloud.microsoft", "microsoft365.com", "office.com")
        keep = next((page for page in pages if any(host in (page.url or "") for host in hosts)), pages[0])
        for page in pages:
            if page is not keep:
                try:
                    page.close()
                except Exception:
                    pass
        try:
            keep.bring_to_front()
        except Exception:
            pass
        return keep
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
    def _triggers(self, page, name=""):
        """Visible submenu triggers of the model menu; the vendor matching `name` comes first."""
        selectors = (
            '[data-test-id^="gptSubMenuModelTrigger"]',
            '[data-testid^="gptSubMenuModelTrigger"]',
            '[role="menuitem"][aria-haspopup="menu"]',
        )
        found, seen = [], set()
        for root in self.roots(page):
            for selector in selectors:
                try:
                    matches = root.locator(selector)
                    for index in range(min(matches.count(), 10)):
                        item = matches.nth(index)
                        if not item.is_visible():
                            continue
                        label = _norm(item.inner_text()) or f"{selector}{index}"
                        if label not in seen:
                            seen.add(label)
                            found.append((item, label))
                except Exception:
                    pass
        key = _norm(name)
        hints = ("gpt", "openai") if "gpt" in key else ("claude", "anthropic") if "claude" in key else ()
        found.sort(key=lambda pair: 0 if any(hint in pair[1] for hint in hints) else 1)
        return [item for item, _ in found]
    def _option(self, page, name=DEFAULT_MODEL, wait_ms=5000):
        """Visible menu row for model `name`: exact title first, then prefix, then substring."""
        name = " ".join((name or DEFAULT_MODEL).split())
        want = _norm(name)
        legacy = name.casefold() == DEFAULT_MODEL.casefold()
        deadline = time.time() + wait_ms / 1000
        best, best_score = None, 0
        while True:
            for root in self.roots(page):
                try:
                    rows = root.locator('[role="menuitemradio"],[role="menuitem"],[role="option"]')
                    texts = rows.all_inner_texts()
                except Exception:
                    continue
                for index, text in enumerate(texts[:60]):
                    lines = text.strip().splitlines()
                    key = _norm(lines[0] if lines else "")
                    if not key:
                        continue
                    score = 4 if key == want else 3 if key.startswith(want) else 2 if want in key else 1 if len(key) >= 6 and want.startswith(key) else 0
                    if score <= best_score:
                        continue
                    try:
                        row = rows.nth(index)
                        if row.is_visible() and (row.get_attribute("aria-haspopup") or "") != "menu":
                            best, best_score = row, score
                    except Exception:
                        pass
                if legacy and best_score < 4:
                    try:
                        mark = root.locator('[data-testid="checkmark-Gpt_5_6_Reasoning"]').first
                        if mark.count():
                            row = mark.locator("xpath=ancestor::*[@role='menuitemradio'][1]").first
                            if row.count() and row.is_visible():
                                best, best_score = row, 4
                    except Exception:
                        pass
            if best_score == 4 or time.time() >= deadline:
                return best
            page.wait_for_timeout(150)
    def _available(self, page):
        names = []
        for root in self.roots(page):
            try:
                for text in root.locator('[role="menuitemradio"],[role="menuitem"],[role="option"]').all_inner_texts():
                    lines = text.strip().splitlines()
                    if lines and lines[0].strip() not in names:
                        names.append(lines[0].strip())
            except Exception:
                pass
        return names[:12]
    def _find_option(self, page, name):
        option = self._option(page, name, 1500)  # newer pickers list models at the top level
        if option:
            return option
        for trigger in self._triggers(page, name):
            try:
                if (trigger.get_attribute("aria-expanded") or "").lower() != "true":
                    try:
                        trigger.hover(timeout=1500)
                        page.wait_for_timeout(250)
                    except Exception:
                        pass
                    if (trigger.get_attribute("aria-expanded") or "").lower() != "true":
                        trigger.click(timeout=3000)
                        page.wait_for_timeout(350)
            except Exception:
                continue
            option = self._option(page, name, 4000)
            if option:
                return option
        return None
    def _dismiss_model_menu(self, page):
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(200)
        except Exception:
            pass
    def _menu_open(self, page):
        if self._triggers(page):
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
            "#gptModeSwitcher",
            'button[id*="ModeSwitcher" i]',
            'button[aria-label="Model Selector"]',
            'button[aria-label*="model" i]',
            'button[aria-label*="m\u00f4 h\u00ecnh" i]',  # Vietnamese UI: "Bộ chọn mô hình"
            'button[aria-label*="Think" i]',
            'button[aria-haspopup="menu"]:has-text("Think")',
            'button[aria-haspopup="menu"]:has-text("GPT")',
            'button[aria-haspopup="menu"]:has-text("Claude")',
        )
        # Sidebar "more" buttons are menu buttons too; never probe them (a stray click can navigate away).
        generic = (
            'button[aria-haspopup="menu"]:not([data-chat-history-more-button-conversation-id])'
            ':not([aria-label="Xem th\u00eam"]):not([aria-label="See more"])'
        )
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
    def _current_model(self, page):
        # The picker button shows the active model ("GPT-5.6 Sol Think"); its label is localized.
        for root in self.roots(page):
            try:
                button = root.locator("#gptModeSwitcher").first
                if button.count() and button.is_visible():
                    return (button.inner_text() or "").strip() or (button.get_attribute("aria-label") or "").split(",")[0].strip()
            except Exception:
                pass
        return ""
    def _select_model_once(self, page, name):
        if _norm(self._current_model(page)) == _norm(name):
            return name  # already active: do not touch the menu
        if not self._open_model_menu(page):
            raise RuntimeError("Model picker not found")
        option = self._find_option(page, name)
        if not option:
            shown = ", ".join(self._available(page)) or "none"
            raise RuntimeError(f"Model {name!r} not found in the picker (visible: {shown})")
        if (option.get_attribute("aria-checked") or "").lower() != "true":
            option.click(timeout=3000)
            page.wait_for_timeout(500)
            self._verify_model(page, name)
        return name
    def _verify_model(self, page, name):
        # Re-open the picker: some builds ignore the first click, so confirm the radio is checked.
        self._dismiss_model_menu(page)
        if not self._open_model_menu(page):
            return
        option = self._find_option(page, name)
        if option and (option.get_attribute("aria-checked") or "").lower() == "false":
            raise RuntimeError(f"Model {name!r} was clicked but is not selected")
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
    def type_prompt(self, page, box, prompt):
        """Put `prompt` into the composer and confirm it is really there before sending.

        The rich-text composer sometimes ignores fill() (notably right after an attachment upload), which left
        only the files in the box - M365 then refuses to send and the old "composer is empty = sent" check
        mistook that for success. Tries fill, then insert_text, then real key presses."""
        probe = _norm(prompt)[:24]
        for attempt in ("fill", "insert", "keys"):
            try:
                box.click()
                if attempt == "fill":
                    box.fill(prompt)
                else:
                    box.press("Control+A")
                    box.press("Backspace")
                    if attempt == "insert":
                        page.keyboard.insert_text(prompt)
                    else:
                        box.press_sequentially(prompt, delay=0)
            except Exception:
                continue
            page.wait_for_timeout(250)
            typed = _norm(self.composer_text(box))
            if typed and (not probe or probe in typed):
                return True
        return False

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
    def resume_chat_page(self, headless):
        # Stay in the tab that already holds the conversation. Only when it is gone (browser
        # restarted, other tab mode) fall back to a fresh chat.
        page = self.open(headless)
        host = (page.url or "").lower()
        if "m365.cloud.microsoft" in host and not self.login_url(host):
            return page, page.url
        return self.open_chat_page(URL, headless), URL
    def conversation_url(self, page):
        # A new chat only gets its own URL a moment after the first answer; None = still the bare /chat page.
        base = URL.split("//", 1)[-1].split("/", 1)[-1].rstrip("/")
        deadline = time.time() + 10
        while True:
            path = (page.url or "").split("//", 1)[-1].split("/", 1)[-1].split("?", 1)[0].split("#", 1)[0].rstrip("/")
            if path and path != base:
                return page.url
            if time.time() >= deadline:
                return None
            page.wait_for_timeout(500)
    def image_srcs(self, page):
        found = []
        script = (
            "() => [...document.images].filter(i => i.complete && i.naturalWidth >= 300 && i.naturalHeight >= 200"
            " && i.offsetParent !== null).map(i => i.currentSrc || i.src)"
        )
        for root in self.roots(page):
            try:
                for src in root.evaluate(script):
                    if src and src not in found and not src.startswith("data:image/svg"):
                        found.append(src)
            except Exception:
                pass
        return found
    def fetch_image(self, page, src):
        script = (
            "async (src) => { const r = await fetch(src, {credentials: 'include'}); if (!r.ok) throw new Error(r.status);"
            " const b = new Uint8Array(await r.arrayBuffer()); let s = '';"
            " for (let i = 0; i < b.length; i += 32768) s += String.fromCharCode.apply(null, b.subarray(i, i + 32768));"
            " return btoa(s); }"
        )
        for root in self.roots(page):
            try:
                return base64.b64decode(root.evaluate(script, src))
            except Exception:
                continue
        return self.ctx.request.get(src).body()
    def collect_images(self, page, before, image_dir, wait_s=180):
        # Image generation lags behind the text answer, so keep polling until a new large image shows up.
        known = set(before)
        deadline = time.time() + wait_s
        while time.time() < deadline:
            new = [src for src in self.image_srcs(page) if src not in known]
            if new and not self.busy(page):
                page.wait_for_timeout(1500)  # let the final render replace any low-res preview
                new = [src for src in self.image_srcs(page) if src not in known] or new
                break
            page.wait_for_timeout(1500)
        else:
            raise RuntimeError("No image was generated in the chat answer")
        Path(image_dir).mkdir(parents=True, exist_ok=True)
        saved = []
        for index, src in enumerate(new[-3:], 1):
            data = self.fetch_image(page, src)
            ext = ".png" if data[:4] == b"\x89PNG" else ".jpg" if data[:2] == b"\xff\xd8" else ".webp" if data[8:12] == b"WEBP" else ".png"
            target = Path(image_dir) / f"infographic-{index}{ext}"
            target.write_bytes(data)
            saved.append(str(target))
        return saved
    def chat(self, prompt, url=None, files=None, headless=True, model=DEFAULT_MODEL, resume=False, image_dir=None):
        self.images = []
        if resume and not url:
            page, target_url = self.resume_chat_page(headless)
        else:
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
            before_images = self.image_srcs(page) if image_dir else []
            box = self.wait_composer(page)
            if not self.type_prompt(page, box, (prompt or "").strip() or DEFAULT_PROMPT):
                raise RuntimeError("PROMPT_NOT_ENTERED: the prompt could not be typed into the M365 composer, "
                                   "so the attachments alone cannot be sent")
            box.press("Enter")
            # Enter alone is not always registered by this composer (multi-line editors,
            # an attachment upload still settling, etc.), so verify the message actually
            # left the composer before assuming it was sent - the prompt was confirmed in the
            # box above, so an empty composer now really means "sent". Only fall back to a
            # second Enter and then the real Send button if that quick check fails.
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
                        if image_dir:
                            self.images = self.collect_images(page, before_images, image_dir)
                        return current, self.conversation_url(page), chosen
            raise RuntimeError(
                "Response timeout: assistant answer card was not detected"
            )
        finally:
            # Keep the context alive (headless OR headed) so it can be reused by the next
            # request via open(). Previously the headed context was closed here, which
            # forced a fresh (re-authenticating) launch on every headed chat.
            pass
