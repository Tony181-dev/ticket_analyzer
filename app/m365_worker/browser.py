"""
Wraps Playwright to drive the M365 Copilot web chat UI (https://m365.cloud.microsoft/chat) in a persistent browser profile, either headless or headed. The Browser class handles opening/closing the browser context, detecting whether the user is authenticated, running an interactive login flow, locating the message
composer across page frames, selecting the required chat model, and sending prompts with attachments while reading back the assistant's response and conversation URL. It is used exclusively by worker.py.
"""
import os
import base64
import re
import shutil
import time
import unicodedata
import mimetypes
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
URL = "https://m365.cloud.microsoft/chat"
PROFILE = Path.home() / ".cache" / "consult_with_copilot" / "browser_profile"
DEFAULT_MODEL = "GPT-5.6 Sol Think Deeper"  # legacy; the app passes the chosen model
MODEL_SELECT_ATTEMPTS = 3
# M365 Copilot will not send attachments on their own: an empty composer keeps Send disabled.
DEFAULT_PROMPT = "Thực hiện phân tích theo các file đính kèm. / Please analyze the attached files."

class BrowserClosedError(RuntimeError):
    pass

def _closed_error(error):
    return isinstance(error, BrowserClosedError) or "has been closed" in str(error).lower()

def _accepts_file(accept, file_path):
    if not accept or not file_path:
        return True
    suffix = Path(file_path).suffix.lower()
    mime = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    return any(value in (suffix, mime, "*/*") or (value.endswith("/*") and mime.startswith(value[:-1]))
               for value in (item.strip().lower() for item in accept.split(",")) if value)
def _fold(text):
    """Lower-case text without accents ("Tư duy sâu" -> "tu duy sau"); Hangul syllables are kept."""
    text = unicodedata.normalize("NFKD", (text or "").replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", text).casefold()
def _norm(text):
    return re.sub(r"[^a-z0-9\uac00-\ud7a3]+", "", _fold(text))

def _same_prompt(text, prompt):
    probe = _norm(prompt)
    return bool(text.strip()) and (_norm(text) == probe if probe else text.strip() == prompt.strip())
# The picker is localized: the same model shows as "GPT-5.6 Sol Think Deeper" (English UI) or
# "GPT-5.6 Sol Tư duy sâu" (Vietnamese UI). Compare labels in one canonical (English) form.
_MODEL_WORDS = (("tuduysau", "thinkdeeper"), ("tuduy", "think"), ("phanhoinhanh", "quickresponse"),
                ("tudong", "auto"))
def _model_key(text):
    key = _norm(text)
    for local, english in _MODEL_WORDS:
        key = key.replace(local, english)
    return key
# Model menu rows. The mode radios ("Tự động" / "Phản hồi nhanh" / "Tư duy sâu", testids -Magic / -Chat /
# -Reasoning) share the gptSubMenuModelTrigger prefix with the vendor submenu triggers: only rows with
# aria-haspopup="menu" open a submenu - the others select a mode the moment they are clicked.
_SUBMENU_TRIGGERS = ('[data-testid^="gptSubMenuModelTrigger"][aria-haspopup="menu"]',
                     '[data-test-id^="gptSubMenuModelTrigger"][aria-haspopup="menu"]')
_MODEL_MENU_ROWS = ('[data-testid^="gptSubMenuModelTrigger"]', '[data-test-id^="gptSubMenuModelTrigger"]',
                    '[role="menuitemradio"]')
# All composer operations share the same scope. The common composer replaced the legacy
# ChatInput wrapper; its attachment tray is 13 ancestors above the editor in the supplied DOM.
_COMPOSER_DOM_JS = r"""
  const shown = el => !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length));
  const findEditor = () => {
    for (const selector of ['#m365-chat-editor-target-element', '[data-lexical-editor="true"][contenteditable="true"]', '[role="textbox"][contenteditable="true"]', 'textarea']) {
      const el = [...document.querySelectorAll(selector)].find(shown);
      if (el) return el;
    }
    return null;
  };
  const composerScope = editor => editor && (
    editor.closest('#m365-chat-input-shared-container, [data-test-id="chat-input-wrapper"], [data-testid="chat-input-wrapper"]')
    || editor.closest('[class*="inputWrapper" i]') || editor.closest('[class*="ChatInput" i]')
    || editor.closest('form') || editor.parentElement);
  const composerFiles = scope => {
    if (!scope) return [];
    const modern = [...scope.querySelectorAll('.fx-Attachment, [id^="composer-attachment-"]')].filter(shown);
    if (modern.length) return modern.filter(el => !modern.some(parent => parent !== el && parent.contains(el)));
    const list = scope.querySelector('[class*="attachments" i]');
    return list ? [...list.children].filter(shown) : [];
  };
  const uploading = scope => !!scope && (scope.matches('[aria-busy="true"]')
    || [...scope.querySelectorAll('[role="progressbar"], [aria-busy="true"], [class*="spinner" i]')].some(shown));
"""
_UPLOAD_STATE_JS = '(name) => {' + _COMPOSER_DOM_JS + r"""
  const editor = findEditor(), scope = composerScope(editor);
  const cards = composerFiles(scope);
  const full = name.toLowerCase(), stem = full.replace(/\.[^.]+$/, '');
  const named = card => [card, ...card.querySelectorAll('*')].some(el =>
    [el.getAttribute('title'), el.getAttribute('aria-label'), el.textContent].some(value => {
      const text = (value || '').toLowerCase();
      return text.includes(full) || (stem.length >= 4 && text.includes(stem));
    }));
  const errors = scope ? [...scope.querySelectorAll('[role="alert"]')].filter(shown).map(el => el.textContent.trim()).filter(Boolean) : [];
  return {scoped: !!scope, chips: cards.filter(named).length, items: cards.length, busy: uploading(scope), errors};
}"""
_DRAFT_STATE_JS = 'el => {' + _COMPOSER_DOM_JS + r"""
  if (!el.isConnected) return null;
  const scope = composerScope(el);
  return {text: el.tagName === 'TEXTAREA' ? el.value : el.innerText,
    attachments: composerFiles(scope).length, uploading: uploading(scope)};
}"""
_ATTACHMENT_BLOCKER_JS = '() => {' + _COMPOSER_DOM_JS + r"""
  const editor = findEditor(), scope = composerScope(editor);
  const texts = [];
  if (scope) for (const el of scope.querySelectorAll('*')) {
    if (!shown(el) || el === editor || editor.contains(el)) continue;
    texts.push(el.getAttribute('title') || '', el.getAttribute('aria-label') || '');
    if (!el.children.length) texts.push(el.textContent || '');
    for (const id of (el.getAttribute('aria-describedby') || '').split(/\s+/)) {
      const description = document.getElementById(id);
      if (description && !editor.contains(description)) texts.push(description.textContent || '');
    }
  }
  for (const el of document.querySelectorAll('[role="alert"], [role="tooltip"]')) {
    if (shown(el) && (!editor || !editor.contains(el))) texts.push(el.textContent || '');
  }
  const blocked = /remove\s+(?:the\s+)?(?:file\s+)?attachments?\s+to\s+send|attachments?.*(?:not supported|unsupported|not allowed)|file (?:type|format).*(?:not supported|unsupported)|(?:cannot|can't|unable to)\s+(?:upload|attach)|file.*too large|(?:xóa|xoá|gỡ|bỏ).*(?:tệp đính kèm|tệp|đính kèm).*(?:gửi)|첨부.*(?:제거|삭제).*전송/i;
  return (texts.find(text => blocked.test(text)) || '').trim().slice(0, 400);
}"""

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
        # Select a chat tab without closing authentication popups or the user's diagnostic tabs.
        pages = [page for page in self.ctx.pages if not page.is_closed()]
        if not pages:
            return self.ctx.new_page()
        hosts = ("m365.cloud.microsoft", "microsoft365.com", "office.com")
        chats = [page for page in pages if urlparse(page.url or "").hostname in hosts]
        current = getattr(self, "page", None)
        # Keep the worker's tab even during a transient SSO redirect; reuse a bootstrap blank
        # page on cold start rather than leaving one more blank tab behind for every request.
        keep = current if current in pages else chats[0] if chats else pages[0]
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
            "#m365-chat-editor-target-element",
            "[data-lexical-editor='true'][contenteditable='true']",
            "[role='textbox'][contenteditable='true']",
            "textarea[placeholder]",
            "textarea",
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
                raise BrowserClosedError("M365_BROWSER_CLOSED: chat page has been closed before the composer was ready")
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
        found, seen = [], set()
        for root in self.roots(page):
            for selector in _SUBMENU_TRIGGERS:
                try:
                    matches = root.locator(selector)
                    for index in range(min(matches.count(), 10)):
                        item = matches.nth(index)
                        if not item.is_visible():
                            continue
                        label = _model_key(item.inner_text()) or f"{selector}{index}"
                        if label not in seen:
                            seen.add(label)
                            found.append((item, label))
                except Exception:
                    pass
        key = _model_key(name)
        hints = ("gpt", "openai") if "gpt" in key else ("claude", "anthropic") if "claude" in key else ()
        found.sort(key=lambda pair: 0 if any(hint in pair[1] for hint in hints) else 1)
        return [item for item, _ in found]
    def _option(self, page, name=DEFAULT_MODEL, wait_ms=5000):
        """Visible menu row for model `name`: exact title first, then prefix, then substring."""
        name = " ".join((name or DEFAULT_MODEL).split())
        want = _model_key(name)
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
                    key = _model_key(lines[0] if lines else "")
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
        """Visible menu rows, for the error message (hidden menus - Share, Settings, Help… - are left out)."""
        names = []
        for root in self.roots(page):
            try:
                rows = root.locator('[role="menuitemradio"],[role="menuitem"],[role="option"]')
                for index in range(min(rows.count(), 40)):
                    row = rows.nth(index)
                    if not row.is_visible():
                        continue
                    lines = (row.inner_text() or "").strip().splitlines()
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
        for root in self.roots(page):
            for selector in _MODEL_MENU_ROWS:
                try:
                    items = root.locator(selector)
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
            ':not([data-testid="PlusMenuButton"])'  # the composer's "+" (add sources) menu
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
    def _active(self, page, name):
        """True / False when the picker button tells whether `name` is the active model (labels compared
        without case, accents or UI language); None when there is no such button."""
        label = self._current_model(page)
        return _model_key(label) == _model_key(name) if label else None
    def _select_model_once(self, page, name):
        if self._active(page, name):
            return name  # already active: do not touch the menu
        if not self._open_model_menu(page):
            raise RuntimeError("Model picker not found")
        option = self._find_option(page, name)
        if not option:
            shown = ", ".join(self._available(page)) or "none"
            raise RuntimeError(f"Model {name!r} not found in the picker (visible: {shown})")
        # A vendor submenu keeps its last model checked while a mode such as "Auto" is the active one, so a
        # checked row is still clicked when the picker button shows another model.
        if (option.get_attribute("aria-checked") or "").lower() != "true" or self._active(page, name) is False:
            option.click(timeout=3000)
            page.wait_for_timeout(500)
            self._verify_model(page, name)
        return name
    def _verify_model(self, page, name):
        # Re-open the picker: some builds ignore the first click, so confirm the radio is checked.
        self._dismiss_model_menu(page)
        if self._active(page, name):
            return
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
    def composer_scope(self, page):
        box = self.composer(page)
        if box is None:
            return None
        for query in (
            "xpath=ancestor::*[@id='m365-chat-input-shared-container' or @data-test-id='chat-input-wrapper' or @data-testid='chat-input-wrapper'][1]",
            "xpath=ancestor::*[contains(@class, 'inputWrapper') or contains(@class, 'ChatInput') or self::form][1]",
        ):
            scope = box.locator(query)
            if scope.count():
                return scope
        return None

    def clear_attachments(self, page):
        """Remove only unsent composer attachments before staging this request's complete file set."""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            self.check_page(page)
            if not self._upload_state(page, "")["items"]:
                return
            scope = self.composer_scope(page)
            if scope is None:
                break
            buttons = scope.locator(".fx-Attachment__dismissButton, [class*='attachments' i] button[aria-label*='remove' i], [class*='attachments' i] button[aria-label*='xóa' i], [class*='attachments' i] button[aria-label*='xoá' i], [class*='attachments' i] button[aria-label*='삭제']")
            button = next((buttons.nth(i) for i in range(buttons.count()) if buttons.nth(i).is_visible()), None)
            if button is None:
                break
            button.click(timeout=2000)
            page.wait_for_timeout(150)
        raise RuntimeError("M365_DRAFT_NOT_CLEARED: old unsent attachments could not be removed; no prompt was sent")

    def file_input(self, page, file_path=None):
        candidates = []
        for root in self.roots(page):
            try:
                matches = root.locator("input[type='file']")
                for index in range(matches.count()):
                    item = matches.nth(index)
                    accept = item.get_attribute("accept") or ""
                    files = file_path if isinstance(file_path, (list, tuple)) else [file_path]
                    if item.is_enabled() and all(_accepts_file(accept, file) for file in files):
                        score = int(bool(accept)) + 2 * (item.get_attribute("id") == "upload-file-button")
                        candidates.append((score, item))
            except Exception:
                pass
        return max(candidates, key=lambda pair: pair[0])[1] if candidates else None

    def check_page(self, page):
        if page.is_closed():
            raise BrowserClosedError("M365_BROWSER_CLOSED: chat page has been closed")

    def attachment_blocker(self, page, reveal=False):
        self.check_page(page)
        for root in self.roots(page):
            try:
                text = root.evaluate(_ATTACHMENT_BLOCKER_JS)
                if text:
                    return text
            except Exception as error:
                if _closed_error(error):
                    raise
        if reveal:
            button = self.send_button(page)
            try:
                if button is not None and button.is_disabled(timeout=1000):
                    button.hover(timeout=1000)
                    page.wait_for_timeout(200)
                    return self.attachment_blocker(page)
            except Exception as error:
                if _closed_error(error):
                    raise
        return ""
    def _upload_state(self, page, name):
        """{chips, items, busy} of the composer area: file chips naming `name`, attachment-list entries and
        whether a visible progress indicator is still spinning there."""
        best = None
        for root in self.roots(page):
            try:
                state = root.evaluate(_UPLOAD_STATE_JS, name)
            except Exception:
                continue
            if state.get("scoped"):
                return state
            best = best or state
        return best or {"chips": 0, "items": 0, "busy": False}
    def upload(self, page, file_path, timeout=None):
        """Upload one file, verifying its own chip rather than an unrelated list change."""
        self._upload_paths(page, [file_path], timeout)

    def upload_files(self, page, files, timeout=None):
        files = list(files or [])
        if not files:
            return
        if len(files) == 1:
            if timeout is None:
                self.upload(page, files[0])
            else:
                self.upload(page, files[0], timeout)
            return
        control = self.file_input(page, files)
        if control is not None and control.get_attribute("multiple") is not None:
            self._upload_paths(page, files, timeout)
        else:
            for file in files:
                self.upload(page, file, timeout)
        self.verify_attachments(page, files)

    def verify_attachments(self, page, files):
        self.check_page(page)
        states = [(Path(file).name, self._upload_state(page, Path(file).name)) for file in files]
        missing = [name for name, state in states if not state["scoped"] or not state["chips"]]
        if missing or any(state["busy"] for _, state in states):
            raise RuntimeError("M365_ATTACHMENT_MANIFEST: attachments are missing or still processing: " + ", ".join(missing or [name for name, _ in states]))
        actual = self._upload_state(page, "")["items"]
        if actual != len(files):
            raise RuntimeError(f"M365_ATTACHMENT_MANIFEST: expected {len(files)} files, found {actual}; no prompt was sent")

    def _upload_paths(self, page, files, timeout=None):
        self.check_page(page)
        file_input = self.file_input(page, files)
        names = [Path(file).name for file in files]
        if file_input is None:
            raise RuntimeError("M365_ATTACHMENT_INPUT: no enabled upload control accepts " + ", ".join(names))
        sizes = {Path(file).name: Path(file).stat().st_size for file in files}
        if timeout is None:
            timeout = min(120, 60 + (sum(sizes.values()) // (256 * 1024)) * 5)
        before = {name: self._upload_state(page, name) for name in names}
        file_input.set_input_files([str(file) for file in files])
        began = time.monotonic()
        ready = 0
        probed = False
        missing = names
        while time.monotonic() - began < timeout:
            self.check_page(page)
            page.wait_for_timeout(400)
            states = {name: self._upload_state(page, name) for name in names}
            missing = [name for name, state in states.items() if state["chips"] <= before[name]["chips"]]
            busy = any(state["busy"] for state in states.values())
            reveal = not probed and not busy and (not missing or time.monotonic() - began >= 1)
            blocker = self.attachment_blocker(page, reveal=reveal)
            probed = probed or reveal
            first = missing[0] if missing else names[0]
            if blocker:
                raise RuntimeError(f"M365_ATTACHMENT_BLOCKED: Attachment rejected: {first} ({sizes[first]:,} B): {blocker}. No prompt was sent")
            failures = [text for state in states.values() for text in state.get("errors", []) if re.search(
                r"failed|error|unsupported|too large|unable|couldn.t|can.t upload|không thể|quá lớn|không hỗ trợ|실패|너무 큽", text, re.I)]
            if failures:
                raise RuntimeError(f"M365_ATTACHMENT_BLOCKED: Attachment rejected: {first} ({sizes[first]:,} B): {failures[0][:300]}")
            ready = ready + 1 if not missing and not busy else 0
            if ready >= 2:
                return
        detail = ", ".join(f"{name} ({sizes[name]:,} B)" for name in missing or names)
        raise RuntimeError(f"Attachment upload did not complete: {detail} (missing chip or still processing after {timeout}s)")

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
        if self.generating(page):
            return True
        # Upload/history widgets can stay aria-busy after sending. Only assistant response
        # containers count here, otherwise a completed answer can wait forever for unrelated UI.
        selector = (
            "[data-message-author-role='assistant'][aria-busy='true'],"
            "[data-message-author-role='assistant'] [aria-busy='true'],"
            "[data-author='assistant'][aria-busy='true'],"
            "[data-author='assistant'] [aria-busy='true'],"
            "[data-testid*='assistant-response' i][aria-busy='true'],"
            "[data-testid*='assistant-response' i] [aria-busy='true']"
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
    def type_prompt(self, page, box, prompt, methods=("fill", "insert", "keys")):
        """Put `prompt` into the composer and confirm it is really there before sending.

        The rich-text composer sometimes ignores fill() (notably right after an attachment upload), which left
        only the files in the box - M365 then refuses to send and the old "composer is empty = sent" check
        mistook that for success. Tries fill, then insert_text, then real key presses."""
        for attempt in methods:
            try:
                # Uploading can replace the Lexical editor; never keep typing into the old node.
                box = self.composer(page) or box
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
            if _same_prompt(self.composer_text(box), prompt):
                return True
        return False

    def ensure_prompt(self, page, prompt):
        box = self.wait_composer(page)
        if box is not None and (_same_prompt(self.composer_text(box), prompt) or self.type_prompt(page, box, prompt)):
            return
        raise RuntimeError("PROMPT_NOT_ENTERED: the full prompt could not be typed into the M365 composer")

    def send_button(self, page):
        """Find Send in the current composer, including tenants using type=button."""
        selectors = (
            "button.fai-SendButton",
            "button[aria-label='G\u1eedi']",
            "button[aria-label='Send']", "button[aria-label*='Send message' i]",
            "button[aria-label='보내기']",
        )
        scopes = []
        box = self.composer(page)
        if box is not None:
            try:
                scope = self.composer_scope(page)
                if scope is not None:
                    scopes.append(scope)
            except Exception:
                pass
        scoped = bool(scopes)
        if not scopes:
            scopes = self.roots(page)
        for root in scopes:
            # An unlabelled submit button is safe only inside the composer, never elsewhere on the page.
            choices = selectors + (("button[type='submit']",) if scoped else ())
            for selector in choices:
                try:
                    matches = root.locator(selector)
                    for index in range(matches.count() - 1, -1, -1):
                        button = matches.nth(index)
                        if button.is_visible():
                            return button
                except Exception:
                    continue
        return None

    def click_send(self, page):
        button = self.send_button(page)
        if button is not None:
            try:
                if button.is_enabled():
                    button.click(timeout=2000)
                    return True
            except Exception:
                pass
        return False

    def draft_state(self, page):
        box = self.composer(page)
        if box is not None:
            try:
                return box.evaluate(_DRAFT_STATE_JS)
            except Exception:
                pass
        # A detached/missing editor is unknown, not an empty (successfully sent) draft.
        return None

    def generating(self, page):
        selectors = (
            "button[aria-label*='Stop generating' i]", "button[aria-label*='Stop responding' i]",
            "button[aria-label*='Dừng tạo' i]", "button[aria-label*='Dừng phản hồi' i]",
            "button[aria-label*='응답 중지']",
        )
        for root in self.roots(page):
            for selector in selectors:
                try:
                    matches = root.locator(selector)
                    if any(matches.nth(index).is_visible() for index in range(matches.count())):
                        return True
                except Exception:
                    continue
        return False

    def send_prompt(self, page, prompt, before, timeout=60, expected_files=None):
        """Wait for upload/Send readiness, then require acknowledgement before waiting for an answer."""
        self.ensure_prompt(page, prompt)
        deadline = time.monotonic() + timeout
        attempted = False
        blank_since = None
        last_action = 0
        refreshed = False
        keyboard_attempts = 0
        while time.monotonic() < deadline:
            self.check_page(page)
            blocker = self.attachment_blocker(page)
            if blocker:
                code = "M365_SEND_UNCERTAIN" if attempted else "M365_ATTACHMENT_BLOCKED"
                raise RuntimeError(f"{code}: {blocker}. Check the selected model/session and attachment policy")
            state = self.draft_state(page)
            # Also accept a manual Send while automation was waiting for file processing.
            if self.generating(page) or len(self._cards(page)) > len(before):
                self._submission_attempted = True
                return
            if attempted:
                if state and not state["text"].strip() and not state["attachments"] and not state["uploading"]:
                    blank_since = blank_since or time.monotonic()
                    if time.monotonic() - blank_since >= 1:
                        return
                else:
                    blank_since = None
            matches_prompt = state and _same_prompt(state["text"], prompt)
            if state and not attempted and not state["text"].strip() and not state["uploading"] and not refreshed:
                current = self.composer(page)
                if current is not None:
                    self.type_prompt(page, current, prompt, methods=("insert", "keys"))
                    refreshed = True
            if matches_prompt and not state["uploading"]:
                now = time.monotonic()
                if not attempted:
                    button = self.send_button(page)
                    try:
                        enabled = button is not None and button.is_enabled(timeout=1000)
                    except Exception:
                        page.wait_for_timeout(250)
                        continue  # M365 replaced the editor/button while checking readiness.
                    if enabled:
                        if expected_files is not None:
                            self.verify_attachments(page, expected_files)
                        # A click can reach M365 even when Playwright subsequently reports a timeout.
                        # Observe the draft before any retry rather than treating that as "not sent".
                        attempted, last_action = True, now
                        self._submission_attempted = True
                        try:
                            button.click(timeout=2000)
                        except Exception:
                            pass
                    elif button is None:
                        # Some tenants expose only the editor shortcut. A disabled Send must be awaited.
                        current = self.composer(page)
                        if current is not None:
                            if expected_files is not None:
                                self.verify_attachments(page, expected_files)
                            attempted, last_action = True, now
                            self._submission_attempted = True
                            try:
                                current.press("Enter", timeout=2000)
                            except Exception:
                                pass
                    elif not refreshed and deadline - now < timeout - 5:
                        # Lexical can show text while its application state is still empty after upload.
                        # Re-enter through keyboard input once, keeping the attachments in place.
                        current = self.composer(page)
                        if current is not None:
                            self.type_prompt(page, current, prompt, methods=("insert", "keys"))
                            refreshed = True
                elif now - last_action >= 8 and keyboard_attempts < 2:
                    # Retry only while the same full draft is still present, never after it disappeared.
                    button = self.send_button(page)
                    if button is not None:
                        try:
                            if button.is_disabled(timeout=1000):
                                page.wait_for_timeout(250)
                                continue
                        except Exception:
                            continue
                    current = self.composer(page)
                    if current is not None:
                        try:
                            current.press("Enter" if keyboard_attempts == 0 else "Control+Enter", timeout=2000)
                            last_action = now
                            keyboard_attempts += 1
                        except Exception:
                            pass
            page.wait_for_timeout(250)
        state = self.draft_state(page)
        detail = "files still processing" if state and state["uploading"] else "Send unavailable or submission not acknowledged"
        raise RuntimeError(f"PROMPT_NOT_SENT: {detail} after {timeout}s; check the M365 chat draft")
    def open_chat_page(self, url, headless):
        page = self.open(headless)
        try:
            fresh_chat = urlparse(url).path.rstrip("/") == "/chat" and bool(self._cards(page))
            if not self.same_conversation(page.url, url) or fresh_chat:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return page
        except Exception as error:
            if not _closed_error(error):
                raise
            self.page = None
            page = self.open(headless)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            return page

    @staticmethod
    def same_conversation(current, target):
        from urllib.parse import parse_qsl

        def key(url):
            parsed = urlparse(url or "")
            query = tuple(sorted((name, value) for name, value in parse_qsl(parsed.query)
                                 if name.casefold() not in ("redirfrom", "login_hint")))
            return parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), query
        return bool(current and target) and key(current) == key(target)
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
            " && i.offsetParent !== null && !i.closest('nav, aside, [role=navigation], #m365-chat-input-shared-container, [data-test-id=chat-input-wrapper], [class*=inputWrapper], .fx-Attachment'))"
            ".map(i => i.currentSrc || i.src)"
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
        response = self.ctx.request.get(src)
        if not response.ok:
            raise RuntimeError(f"Generated image download failed: HTTP {response.status}")
        return response.body()
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
        errors = []
        for src in reversed(new):
            try:
                data = self.fetch_image(page, src)
                ext = ".png" if data.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg" if data.startswith(b"\xff\xd8") else ".webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else None
                if ext is None:
                    raise RuntimeError("Generated image download is not PNG, JPEG or WebP")
                target = Path(image_dir) / ("infographic-1" + ext)
                target.write_bytes(data)
                return [str(target)]
            except Exception as error:
                errors.append(str(error))
        raise RuntimeError("Could not download the generated infographic: " + "; ".join(errors))
    def chat(self, prompt, url=None, files=None, headless=True, model=DEFAULT_MODEL, resume=False, image_dir=None):
        self._request_url = url
        for attempt in range(2):
            self._submission_attempted = False
            try:
                return self._chat_once(prompt, url, files, headless, model, resume, image_dir)
            except Exception as error:
                if not _closed_error(error):
                    raise
                if self._submission_attempted:
                    raise RuntimeError("M365_SEND_UNCERTAIN: the chat closed after a send attempt; verify the conversation before retrying to avoid duplicate prompts") from error
                if attempt:
                    raise BrowserClosedError("M365_BROWSER_CLOSED: chat closed again before sending after one recovery attempt") from error
                # open() reuses a surviving context or recreates a closed one. Keep the same mode,
                # requested model and conversation, without closing other tabs or forcing sign-in.
                self.page = None
                url = self._request_url or url
                resume = False

    def _chat_once(self, prompt, url=None, files=None, headless=True, model=DEFAULT_MODEL, resume=False, image_dir=None):
        self.images = []
        if resume and not url:
            page, target_url = self.resume_chat_page(headless)
        else:
            target_url = url or URL
            page = self.open_chat_page(target_url, headless)
        self._request_url = target_url
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
            prompt = (prompt or "").strip() or DEFAULT_PROMPT
            # Keep a visible, valid message while file upload/indexing is pending. Previously all
            # uploads had to complete before typing, leaving an empty, unsendable draft on failure.
            self.ensure_prompt(page, prompt)
            self.clear_attachments(page)
            self.upload_files(page, files)
            self.verify_attachments(page, files or [])
            self.ensure_prompt(page, prompt)
            before = self._cards(page)
            before_images = self.image_srcs(page) if image_dir else []
            self.send_prompt(page, prompt, before, expected_files=files or [])
            if image_dir:
                # Image-only replies may have no readable assistant card at all. Wait for the
                # generated raster directly rather than waiting four minutes for text first.
                self.images = self.collect_images(page, before_images, image_dir, wait_s=240)
                cards = self._cards(page)
                answer = cards[-1] if cards and (len(cards) > len(before) or cards[-1] != before[-1]) else "Generated infographic image downloaded."
                return answer, self.conversation_url(page), chosen
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
                        return current, self.conversation_url(page), chosen
            raise RuntimeError(
                "Response timeout: assistant answer card was not detected"
            )
        finally:
            # Keep the context alive (headless OR headed) so it can be reused by the next
            # request via open(). Previously the headed context was closed here, which
            # forced a fresh (re-authenticating) launch on every headed chat.
            pass
