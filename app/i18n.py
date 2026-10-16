"""Server-side messages in Vietnamese and English (settings["lang"]).

Notes already stored in a run keep the language they were written in.
"""
from . import settings

MESSAGES = {
    # pipeline: steps / errors / notes / log lines
    "step.jira": ("Jira ticket", "Jira ticket"),
    "step.understand": ("AI hiểu ticket", "AI reads ticket"),
    "step.log": ("Log brief", "Log brief"),
    "step.graph": ("Code graph", "Code graph"),
    "step.analyze": ("AI phân tích", "AI analysis"),
    "step.format": ("AI format", "AI format"),
    "step.collab": ("Đăng Collab", "Publish to Collab"),
    "err.issue": ("Nhập Jira key hoặc URL hợp lệ, ví dụ ABC-1234", "Enter a valid Jira key or URL, e.g. ABC-1234"),
    "err.log_missing": ("Không tìm thấy file log: {path}", "Log file not found: {path}"),
    "err.src_missing": ("Không tìm thấy folder source: {path}", "Source folder not found: {path}"),
    "err.not_editable": ("File này không sửa được", "This file cannot be edited"),
    "err.interrupted": ("Bị gián đoạn (server đã khởi động lại)", "Interrupted (the server was restarted)"),
    "err.busy": ("Đang có một luồng chạy. Hãy dừng nó trước.", "A run is already in progress. Stop it first."),
    "err.mode_busy": ("Không đổi chế độ AI khi đang chạy", "Cannot switch AI mode while a run is in progress"),
    "err.jira": ("Jira trả lỗi: {detail}", "Jira returned an error: {detail}"),
    "err.no_content": ("không có nội dung", "no content"),
    "err.need_jira": ("Chạy bước Jira trước", "Run the Jira step first"),
    "err.need_analyze": ("Chạy bước AI phân tích trước", "Run the AI analysis step first"),
    "err.need_format": ("Chạy bước AI format trước", "Run the AI format step first"),
    "err.no_space": ("Chưa có Collab space key (⚙ Cấu hình)", "Collab space key is not set (⚙ Settings)"),
    "err.picker": ("Không mở được hộp thoại chọn file (thiếu Tkinter?). Hãy dán đường dẫn vào ô.",
                   "Could not open the file dialog (Tkinter missing?). Paste the path instead."),
    "note.review": ("Kiểm tra report rồi bấm 'Đăng lên Collab'", "Review the report, then click 'Publish to Collab'"),
    "note.stopped": ("Đã dừng", "Stopped"),
    "note.skip_nolog": ("Không có file log", "No log files"),
    "note.skip_nosrc": ("Không chọn folder source", "No source folder"),
    "note.window": ("Khung giờ {start} → {end} ({confidence})", "Window {start} → {end} ({confidence})"),
    "note.nowindow": ("Không xác định được giờ — dùng toàn bộ log", "No usable time — using the whole log"),
    "note.log": ("{total:,} → {kept:,} dòng (~{tokens:,} token)", "{total:,} → {kept:,} lines (~{tokens:,} tokens)"),
    "note.graph": ("{files} file · {nodes} node · {edges} cạnh", "{files} files · {nodes} nodes · {edges} edges"),
    "note.analyze": ("{chars:,} ký tự, dùng {sources} nguồn bằng chứng", "{chars:,} chars from {sources} evidence source(s)"),
    "note.format": ("XHTML hợp lệ ({mode}), {chars:,} ký tự", "Valid XHTML ({mode}), {chars:,} chars"),
    "note.format_local": ("sửa cục bộ", "local repair"),
    "note.format_ai": ("refine bằng AI", "AI refine"),
    "note.attachments": ("{n} tệp đính kèm", "{n} attachment(s)"),
    "note.att_logs": ("+{n} file log từ đính kèm", "+{n} log file(s) from attachments"),
    "log.att_download": ("Tải {n} tệp đính kèm vào {folder}", "Downloading {n} attachment(s) to {folder}"),
    "log.att_added": ("Đã thêm {n} file log vào bước phân tích log", "Added {n} log file(s) to the log step"),
    "note.created": ("Đã tạo trang", "Page created"),
    "note.updated": ("Đã cập nhật trang", "Page updated"),
    "log.saved": ("Đã lưu {name} (các bước sau được đánh dấu cần chạy lại)", "Saved {name} (later steps marked for re-run)"),
    "log.scan": ("Quét source để lập catalog log statement…", "Scanning source for log statements…"),
    "log.window_empty": ("Khung giờ không có dòng nào khớp → cắt lại trên toàn bộ log",
                         "No lines in the time window → re-slicing the whole log"),
    "log.decode": ("Decode cục bộ: {error} → refine trên phản hồi gốc", "Local decode: {error} → refining the raw answer"),
    "log.local_ok": ("Format hợp lệ sau khi sửa cục bộ (không cần gọi AI refine)", "Format valid after local repair (no AI refine needed)"),
    "log.local_fail": ("Sửa cục bộ chưa đủ ({error}) → refine từng đoạn bằng AI", "Local repair not enough ({error}) → AI refine per fragment"),
    # doctor
    "dr.node": ("Node.js ≥ 18 (LogSleuth, Copilot CLI)", "Node.js ≥ 18 (LogSleuth, Copilot CLI)"),
    "dr.node_missing": ("Chưa cài Node.js", "Node.js is not installed"),
    "dr.uv": ("uv / uvx (chạy mcp-atlassian)", "uv / uvx (runs mcp-atlassian)"),
    "dr.uv_missing": ("Chưa cài uv", "uv is not installed"),
    "dr.no_token": ("Chưa có token", "No token yet"),
    "dr.playwright": ("Playwright (điều khiển M365)", "Playwright (drives M365)"),
    "dr.installed": ("đã cài", "installed"),
    "dr.not_installed": ("chưa cài", "not installed"),
    "dr.chromium": ("Chromium cho Playwright", "Chromium for Playwright"),
    "dr.chromium_missing": ("Chưa tải Chromium", "Chromium not downloaded"),
    "dr.m365_login": ("Đăng nhập M365 Copilot", "M365 Copilot sign-in"),
    "dr.unchecked": ("Chưa kiểm tra", "Not checked yet"),
    "dr.logged_in": ("Đã đăng nhập", "Signed in"),
    "dr.need_login": ("Cần đăng nhập", "Sign-in required"),
    "dr.copilot": ("GitHub Copilot CLI", "GitHub Copilot CLI"),
    "dr.copilot_login": ("Đăng nhập Copilot CLI", "Copilot CLI sign-in"),
    "dr.cp_env": ("Dùng token trong biến môi trường", "Using a token from environment variables"),
    "dr.cp_verified": ("Đã xác nhận bằng một lệnh thử", "Verified with a test prompt"),
    "dr.cp_gh": ("Dùng đăng nhập của GitHub CLI (gh)", "Using the GitHub CLI (gh) sign-in"),
    "dr.cp_store": ("Tìm thấy thông tin đăng nhập Copilot", "Copilot credentials found"),
    "dr.cp_missing": ("Chưa đăng nhập — bấm 'Đăng nhập Copilot' hoặc 'Kiểm tra thật'",
                      "Not signed in — click 'Sign in to Copilot' or 'Verify now'"),
    "dr.cp_checking": ("Đang kiểm tra Copilot CLI bằng một lệnh thử…", "Verifying Copilot CLI with a test prompt…"),
    "dr.cp_ok": ("Copilot CLI: đăng nhập OK", "Copilot CLI: sign-in OK"),
    "dr.cp_fail": ("Copilot CLI chưa dùng được: {error}", "Copilot CLI not usable yet: {error}"),
    "dr.cp_wait": ("Đã mở cửa sổ đăng nhập Copilot — app sẽ tự kiểm tra khi cửa sổ đóng.",
                   "Copilot sign-in window opened — the app re-checks when it closes."),
    "dr.cannot_fix": ("Không tự cài được '{id}' trên máy này — hãy cài tay.", "Cannot install '{id}' automatically on this machine — install it manually."),
    "dr.retry": ("↻ Thử lại theo cách khác (mạng có thể chặn SSL)…", "↻ Retrying another way (network may intercept SSL)…"),
    "dr.fixed": ("✓ {id} xong", "✓ {id} done"),
    "dr.mcp_check": ("Kiểm tra kết nối MCP {role}…", "Checking MCP {role} connection…"),
    "dr.mcp_missing_tools": ("thiếu tool {tools}", "missing tools {tools}"),
    "dr.mcp_error": ("lỗi: {error}", "error: {error}"),
    "dr.m365_opening": ("Mở Chromium để đăng nhập M365 (tối đa 5 phút)…", "Opening Chromium for M365 sign-in (up to 5 minutes)…"),
    "srv.running": ("Ticket Analyzer đang chạy tại {url}  (Ctrl+C để dừng)", "Ticket Analyzer is running at {url}  (Ctrl+C to stop)"),
}


def lang():
    return "en" if settings.load().get("lang") == "en" else "vi"


def t(key, **kw):
    pair = MESSAGES.get(key)
    if not pair:
        return key
    text = pair[1] if lang() == "en" else pair[0]
    return text.format(**kw) if kw else text
