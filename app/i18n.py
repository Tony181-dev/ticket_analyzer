"""Server-side messages in Vietnamese, English and Korean (settings["lang"]).

MESSAGES holds (vi, en) pairs; Korean lives in i18n_ko.KO and falls back to English.
Step notes and errors are stored as text in the language they were written in; relocalize() renders them
again in the current UI language when a run is sent to the browser.
"""
import re
from functools import lru_cache

from . import settings
from .i18n_ko import KO

MESSAGES = {
    # pipeline: steps / errors / notes / log lines
    "step.jira": ("Jira ticket", "Jira ticket"),
    "step.understand": ("AI hiểu ticket", "AI reads ticket"),
    "step.log": ("Log brief", "Log brief"),
    "step.graph": ("Code graph", "Code graph"),
    "step.analyze": ("AI phân tích", "AI analysis"),
    "step.format": ("AI format", "AI format"),
    "step.infographic": ("AI infographic", "AI infographic"),
    "step.collab": ("Đăng Collab", "Publish to Collab"),
    "infographic.brand": ("PHÂN TÍCH SỰ CỐ", "INCIDENT ANALYSIS"),
    "infographic.summary": ("TÓM TẮT", "SUMMARY"),
    "infographic.root_cause": ("NGUYÊN NHÂN GỐC", "ROOT CAUSE"),
    "infographic.timeline": ("DÒNG THỜI GIAN", "TIMELINE"),
    "infographic.evidence": ("BẰNG CHỨNG", "EVIDENCE"),
    "infographic.actions": ("HÀNH ĐỘNG ĐỀ XUẤT", "RECOMMENDED ACTIONS"),
    "infographic.not_stated": ("Không nêu trong phân tích", "Not stated in the analysis"),
    "infographic.no_items": ("Không có dữ liệu được nêu.", "No details were stated."),
    "note.tz_wait": ("Chờ bạn chọn múi giờ để căn giờ log", "Waiting for you to pick the time zone for the log clock"),
    "note.tz_unknown": ("⚠ múi giờ chưa rõ", "⚠ time zone not stated"),
    "log.tz_wait": ("Ticket không nêu múi giờ của giờ sự cố — chọn cách căn giờ log (gợi ý / tự nhập / bỏ lọc giờ) rồi chạy tiếp",
                    "The ticket states no time zone for the incident time — choose the log clock alignment (suggested / your own / no time filter) to continue"),
    "log.tz_auto_unknown": ("Ticket không nêu múi giờ: giờ log được căn tự động, nên kiểm tra lại 03-log-span.md",
                            "The ticket states no time zone: the log clock was aligned automatically — check 03-log-span.md"),
    "log.tz_decided": ("Múi giờ: {how}", "Time zone: {how}"),
    "log.tz_bypass": ("Bỏ lọc theo giờ theo lựa chọn của bạn — dùng toàn bộ khoảng giờ của log", "Time filter dropped as you chose — the full log range is used"),
    "err.tz_mode": ("Cách xử lý múi giờ không hợp lệ", "Unknown time-zone choice"),
    "err.tz_shift": ("Cần độ lệch giờ log (phút)", "A log clock shift (minutes) is required"),
    "err.tz_offset": ("Múi giờ phải dạng +07:00 / -05:00", "The time zone must look like +07:00 / -05:00"),
    "err.att_incomplete": ("Archive thiếu part hoặc thiếu part đầu — hãy chọn đủ bộ", "Archive parts are missing, including possibly the first part — select a complete set"),
    "log.report_lang": ("Report được viết bằng {was}, giao diện đang là {now} — chạy lại từ bước AI phân tích để có report đúng ngôn ngữ",
                        "The report was written in {was} but the UI is now {now} — re-run from AI analysis to get it in that language"),
    "tz.auto": ("để hệ thống tự căn", "let the app align"),
    "tz.suggested": ("theo gợi ý", "suggested alignment"),
    "tz.manual": ("tự nhập", "set by you"),
    "tz.bypass": ("bỏ lọc theo giờ", "no time filter"),
    "asst.busy":("Trợ lý đang trả lời câu trước, chờ chút hoặc bấm Dừng", "The assistant is still answering; wait or press Stop"),
    "asst.empty": ("Hãy nhập câu hỏi", "Type a question first"),
    "asst.stopped": ("Đã dừng", "Stopped"),
    "asst.no_text": ("(Trợ lý không trả lời bằng văn bản)", "(The assistant returned no text)"),
    "asst.no_action": ("Không tìm thấy hành động này", "This action no longer exists"),
    "asst.done_already": ("Hành động này đã được thực hiện", "This action was already done"),
    "asst.gate_gone": ("Bước chọn bằng chứng không còn chờ nữa", "The evidence gate is no longer waiting"),
    "asst.need_run": ("Cần mở một ticket trước", "Open a ticket first"),
    "asst.auto_gate": ("🔔 {issue}: đang chờ chọn bằng chứng — tự quét gợi ý", "🔔 {issue}: evidence gate is waiting — auto-scan"),
    "asst.auto_error": ("🔔 Bước \"{step}\" vừa lỗi — tự chẩn đoán", "🔔 Step \"{step}\" just failed — auto-diagnose"),
    "asst.auto_tz": ("🔔 {issue}: ticket không nêu múi giờ — tự gợi ý cách căn giờ log", "🔔 {issue}: no time zone in the ticket — auto-suggest the log clock"),
    "asst.act.resolve_timezone": ("Áp dụng múi giờ & cắt log", "Apply time zone & slice logs"),
    "asst.res.tz": ("Đã áp dụng, bước Log chạy lại", "Applied, the Log step runs again"),
    "asst.scan_msg": ("Quét toàn bộ hệ thống và đề xuất", "Scan the whole system and suggest"),
    "asst.act.download_attachments": ("Tải attachment", "Download attachments"),
    "asst.act.resolve_gate": ("Xác nhận bằng chứng & tiếp tục", "Confirm evidence & continue"),
    "asst.act.run_steps": ("Chạy luồng", "Run the workflow"),
    "asst.act.log_scan": ("Quét lại catalog log", "Rebuild the log catalog"),
    "asst.act.update_inputs": ("Cập nhật tuỳ chọn ticket", "Update ticket options"),
    "asst.act.update_settings": ("Cập nhật cấu hình", "Update settings"),
    "asst.act.create_bug": ("Tạo bug ticket", "Create bug ticket"),
    "asst.act.open_folder": ("Mở thư mục", "Open folder"),
    "asst.act.stop_pipeline": ("Dừng luồng", "Stop the workflow"),
    "asst.res.download": ("Đang tải {n} file (xem Log)", "Downloading {n} file(s) (see Log)"),
    "asst.res.gate": ("Đã xác nhận, luồng tiếp tục", "Confirmed, the workflow continues"),
    "asst.res.started": ("Đã bắt đầu", "Started"),
    "asst.res.saved": ("Đã lưu", "Saved"),
    "asst.res.bug": ("Đã tạo {file}", "Created {file}"),
    "asst.res.bug_dup": ("Đã có ticket trùng: {file}", "A matching ticket exists: {file}"),
    "asst.res.stopped": ("Đã gửi lệnh dừng", "Stop requested"),
    "infographic.code": ("VỊ TRÍ TRONG CODE", "CODE LOCATION"),
    "infographic.severity": ("MỨC ĐỘ", "SEVERITY"),
    "infographic.sev.critical": ("RẤT NGHIÊM TRỌNG", "CRITICAL"),
    "infographic.sev.high": ("CAO", "HIGH"),
    "infographic.sev.medium": ("TRUNG BÌNH", "MEDIUM"),
    "infographic.sev.low": ("THẤP", "LOW"),
    "infographic.confidence": ("ĐỘ TIN CẬY", "CONFIDENCE"),
    "infographic.conf.high": ("CAO", "HIGH"),
    "infographic.conf.medium": ("TRUNG BÌNH", "MEDIUM"),
    "infographic.conf.low": ("THẤP", "LOW"),
    "err.issue": ("Nhập Jira key hoặc URL hợp lệ, ví dụ ABC-1234", "Enter a valid Jira key or URL, e.g. ABC-1234"),
    "err.log_missing": ("Không tìm thấy file log: {path}", "Log file not found: {path}"),
    "err.src_missing": ("Không tìm thấy folder source: {path}", "Source folder not found: {path}"),
    "err.need_catalog_source": ("Cần chọn folder source để tạo catalog cho LogSleuth", "Select a source folder to build the LogSleuth catalog"),
    "err.not_editable": ("File này không sửa được", "This file cannot be edited"),
    "err.interrupted": ("Bị gián đoạn (server đã khởi động lại)", "Interrupted (the server was restarted)"),
    "err.busy": ("Đang có một luồng chạy. Hãy dừng nó trước.", "A run is already in progress. Stop it first."),
    "err.mode_busy": ("Không đổi chế độ AI khi đang chạy", "Cannot switch AI mode while a run is in progress"),
    "err.jira": ("Jira trả lỗi: {detail}", "Jira returned an error: {detail}"),
    "err.no_content": ("không có nội dung", "no content"),
    "err.need_jira": ("Chạy bước Jira trước", "Run the Jira step first"),
    "err.need_analyze": ("Chạy bước AI phân tích trước", "Run the AI analysis step first"),
    "err.need_format": ("Chạy bước AI format trước", "Run the AI format step first"),
    "err.gate": ("Chế độ xử lý phải là skip, review hoặc manual", "The handling mode must be skip, review or manual"),
    "err.nothing_to_resume": ("Mọi bước đã xong — không còn gì để chạy tiếp", "Every step is done — nothing left to continue"),
    "err.until_before_start": ("Bước kết thúc phải nằm sau bước bắt đầu", "The last step must come after the first step"),
    "err.time_format": ("Giờ phải có dạng HH:MM hoặc HH:MM:SS[.mmm]", "Times must look like HH:MM or HH:MM:SS[.mmm]"),
    "err.data_dir_relative": ("Folder lưu trữ thông tin ticket phải là đường dẫn đầy đủ (vd. D:\\Tickets\\ABC-123): {path}",
                              "The ticket data folder must be a full path (e.g. D:\\Tickets\\ABC-123): {path}"),
    "err.data_dir": ("Không tạo được folder lưu trữ thông tin ticket {path}: {error}",
                     "Cannot create the ticket data folder {path}: {error}"),
    "log.data_dir": ("Dữ liệu ticket lưu tại {path} (tệp đính kèm: attachments\\)",
                     "Ticket data is stored in {path} (attachments: attachments\\)"),
    "log.extra_windows": ("Thêm khoảng giờ tự nhập (giờ log): {windows}", "Extra time ranges typed by hand (log clock): {windows}"),
    "err.att_download": ("Không tải được tệp đính kèm: {error}", "Could not download the attachments: {error}"),
    "err.no_logs_available": ("Chưa có file log nào để phân tích — chọn tệp đính kèm khác, thêm file log, hoặc bỏ qua log",
                              "No log file to analyze yet — pick other attachments, add log files, or continue without logs"),
    "err.no_space": ("Chưa có Collab space key (⚙ Cấu hình)", "Collab space key is not set (⚙ Settings)"),
    "err.picker": ("Không mở được hộp thoại chọn file (thiếu Tkinter?). Hãy dán đường dẫn vào ô.",
                   "Could not open the file dialog (Tkinter missing?). Paste the path instead."),
    "note.review": ("Kiểm tra report rồi bấm 'Đăng lên Collab'", "Review the report, then click 'Publish to Collab'"),
    "note.stopped": ("Đã dừng", "Stopped"),
    "note.stopped_kept": ("Đã dừng lần chạy lại — giữ kết quả trước", "Re-run stopped — previous result kept"),
    "log.stopped_kept": ("■ Đã dừng {step} — giữ kết quả của lần chạy trước", "■ Stopped {step} — the previous result is kept"),
    "note.skip_nolog": ("Không có file log", "No log files"),
    "note.skip_nosrc": ("Không chọn folder source", "No source folder"),
    "note.skip_no_attached_logs": ("Ticket không có log đính kèm và chưa chọn file log", "The ticket has no log attachments and no log file was chosen"),
    "note.skip_no_logs_found": ("Không lấy được file log nào — bỏ qua, phân tích không có log",
                                "No log file could be obtained — skipped, analysis runs without logs"),
    "note.skip_logs_user": ("Bạn đã chọn bỏ qua log", "You chose to continue without logs"),
    "note.skip_step_user": ("Bạn đã chọn bỏ qua bước này", "You chose to skip this step"),
    "note.skipping_step": ("Đang dừng tác vụ để bỏ qua bước này…", "Stopping the task before skipping this step…"),
    "note.skip_manual_gate": ("Manual: bạn tự nhập khung giờ ở bước Log", "Manual: you enter the time window at the Log step"),
    "note.gate_review": ("Chờ bạn duyệt: tệp log AI đề xuất, khung giờ và múi giờ", "Waiting for your review: AI-suggested log files, time window and time zone"),
    "note.gate_manual": ("Chờ bạn nhập: tệp log cần tải, khung giờ và múi giờ", "Waiting for your input: log files to download, time window and time zone"),
    "note.picks": ("AI xếp ưu tiên {n} tệp đính kèm", "AI prioritized {n} attachment(s)"),
    "note.evidence_plan": ("Đề xuất tải {n} tệp trong giới hạn tổng dung lượng", "Proposed {n} files within the total download budget"),
    "note.log_shift": ("log lệch {shift} so với giờ trong ticket", "log clock is {shift} from the ticket time"),
    "note.log_full_fallback": ("khung giờ rỗng → đã quét toàn bộ log", "empty window → scanned the full log"),
    "note.graph_empty": ("Folder source không có file ngôn ngữ được hỗ trợ (Python, C/C++, Java, Kotlin, JS/TS)",
                         "The source folder has no file in a supported language (Python, C/C++, Java, Kotlin, JS/TS)"),
    "note.graph_focus": ("{n} symbol liên quan ticket được ưu tiên", "{n} ticket-related symbols ranked first"),
    "note.infographic_fallback": ("M365 không tạo được ảnh → dùng SVG dựng cục bộ", "M365 image failed → local SVG used instead"),
    "note.no_image": ("đăng không kèm infographic (bước 7 chưa có ảnh)", "published without the infographic (step 7 has no image)"),
    "note.skip_graph_disabled": ("Đã tắt Code Graph", "Code Graph is disabled"),
    "note.skip_infographic_disabled": ("Đã chọn Skip", "Skipped by toggle"),
    "err.no_image": ("AI không trả về ảnh hợp lệ", "AI returned no valid image"),
    "note.infographic": ("Đã tạo {name} ({kb} KB)", "Created {name} ({kb} KB)"),
    "note.infographic_failed": ("Không tạo được infographic: {error}", "Infographic failed: {error}"),
    "note.with_image": ("kèm infographic", "with infographic"),
    "note.image_failed": ("không đính được infographic", "infographic could not be attached"),
    "note.window": ("Khung giờ {start} → {end} ({confidence})", "Window {start} → {end} ({confidence})"),
    "note.nowindow": ("Ticket không nêu giờ xảy ra issue — không lọc log theo thời gian", "No incident time in the ticket — no time filter is applied to the logs"),
    "note.log": ("{total:,} → {kept:,} dòng (cận trên {tokens:,} token)", "{total:,} → {kept:,} lines (upper bound {tokens:,} tokens)"),
    "note.log_manual": ("Manual: quét source hoặc chọn catalog, chọn service/khung giờ rồi bấm Slice", "Manual: scan source or choose a catalog, select services/time, then Slice"),
    "note.log_catalog_ready": ("Catalog sẵn sàng — chọn service và bấm Slice", "Catalog ready — select services and Slice"),
    "err.log_manual_catalog": ("Manual cần catalog: bấm Scan source hoặc chọn catalog đã có", "Manual requires a catalog: scan source or select an existing catalog"),
    "err.log_reslice": ("Log brief cũ vượt token cap hiện tại — hãy Slice lại bước Log brief trước khi phân tích", "The saved log brief exceeds the current cap — re-slice the Log brief before analysis"),
    "note.log_no_catalog": ("không có catalog source — dùng từ khóa và tín hiệu lỗi", "no source catalog — using keywords and error signals"),
    "note.graph": ("{files} file · {nodes} node · {edges} cạnh", "{files} files · {nodes} nodes · {edges} edges"),
    "note.analyze": ("{chars:,} ký tự, dùng {sources} nguồn bằng chứng", "{chars:,} chars from {sources} evidence source(s)"),
    "note.format": ("XHTML hợp lệ ({mode}), {chars:,} ký tự", "Valid XHTML ({mode}), {chars:,} chars"),
    "note.format_local": ("sửa cục bộ", "local repair"),
    "note.format_ai": ("refine bằng AI", "AI refine"),
    "note.attachments": ("{n} tệp đính kèm", "{n} attachment(s)"),
    "note.att_logs": ("+{n} file log từ đính kèm", "+{n} log file(s) from attachments"),
    "note.pick_att": ("Chọn tệp đính kèm cần tải", "Choose the attachments to download"),
    "note.att_download": ("Đang tải tệp đính kèm {done}/{total} {name}", "Downloading attachments {done}/{total} {name}"),
    "note.att_extract": ("Đang giải nén {done}/{total} {name}", "Extracting {done}/{total} {name}"),
    "err.att_none": ("Chưa chọn tệp nào", "No file selected"),
    "log.att_download": ("Tải {n} tệp đính kèm vào {folder}", "Downloading {n} attachment(s) to {folder}"),
    "log.att_split_volumes": ("Đã chọn {selected} tệp; tự thêm {added} part còn lại của archive (tổng {total} tệp)",
                              "Selected {selected} file(s); added {added} other archive volume(s) (total {total})."),
    "log.att_added": ("Đã thêm {n} file log vào bước phân tích log", "Added {n} log file(s) to the log step"),
    "log.att_auto_ai": ("AI chọn tải: {names}", "AI chose to download: {names}"),
    "log.att_auto_heuristic": ("Tự chọn ảnh và log/archive trong giới hạn dung lượng: {names}",
                               "Selected images and logs/archives within the download budget: {names}"),
    "log.att_none_picked": ("AI không thấy tệp đính kèm nào là log của sự cố này", "The AI found no attachment that looks like a log for this incident"),
    "log.att_over_cap": ("Không tự tải {name}: vượt giới hạn {mb} MB, thiếu part hoặc chưa rõ dung lượng (⚙ Cấu hình)",
                         "Not auto-downloading {name}: over {mb} MB, missing parts or unknown size (⚙ Settings)"),
    "log.att_failed": ("Không tải được tệp đính kèm, chạy tiếp với log hiện có: {error}", "Attachment download failed, continuing with the logs at hand: {error}"),
    "log.gate_ok": ("Đã duyệt: tải {n} tệp đính kèm rồi chạy tiếp", "Confirmed: downloading {n} attachment(s), then continuing"),
    "log.gate_skip": ("Đã chọn bỏ qua log — chạy tiếp không có log", "Continuing without logs, as chosen"),
    "log.ai_retry": ("↻ AI lỗi tạm thời, thử lại {n}/{total}: {error}", "↻ Transient AI failure, retry {n}/{total}: {error}"),
    "log.align": ("Căn giờ log: {shift} → khung giờ trong log {start}–{end} ({why})", "Log clock alignment: {shift} → window in log clock {start}–{end} ({why})"),
    "log.infographic_fallback": ("M365 không tạo được ảnh ({error}) → dựng infographic SVG cục bộ", "M365 image generation failed ({error}) → drawing the infographic locally as SVG"),
    "align.none": ("không có giờ", "no time"),
    "align.same-clock": ("log cùng giờ với ticket", "log uses the ticket's clock"),
    "align.log-utc": ("log ghi giờ UTC, ticket nêu múi giờ", "log prints UTC, ticket states its zone"),
    "align.log-utc-jira-zone": ("log ghi giờ UTC, ticket theo múi giờ của Jira", "log prints UTC, ticket uses Jira's zone"),
    "align.log-local-jira-zone": ("ticket ghi UTC, log theo múi giờ của Jira", "ticket is UTC, log uses Jira's zone"),
    "align.manual": ("bạn đặt tay", "set by you"),
    "align.ai": ("AI suy ra từ khoảng giờ của log", "inferred by the AI from the log time ranges"),
    "note.created": ("Đã tạo trang", "Page created"),
    "note.updated": ("Đã cập nhật trang", "Page updated"),
    "log.saved": ("Đã lưu {name} (các bước sau được đánh dấu cần chạy lại)", "Saved {name} (later steps marked for re-run)"),
    "log.scan": ("Quét source để lập catalog log statement…", "Scanning source for log statements…"),
    "log.no_catalog": ("Không có source folder — dùng từ khóa ticket và tín hiệu lỗi để tìm log", "No source folder — using ticket keywords and error signals to find log evidence"),
    "log.rewind_stale": ("Chạy lại từ bước {step} vì dữ liệu trước {requested} đã stale", "Re-running from {step} because an upstream artifact before {requested} is stale"),
    "log.continue_optional_error": ("Tiếp tục sau lỗi ở {step} vì đã bật tuỳ chọn", "Continuing after {step} failed because the option is enabled"),
    "log.prep_enc": ("⇄ {name}: {enc} → UTF-8", "⇄ {name}: {enc} → UTF-8"),
    "log.prep_json": ("⇄ {name}: JSON → JSON Lines ({n:,} bản ghi)", "⇄ {name}: JSON → JSON Lines ({n:,} records)"),
    "log.prep_skip": ("⚠ Bỏ qua {name}: {why}", "⚠ Skipped {name}: {why}"),
    "log.why_zstd": ("định dạng .zst chưa hỗ trợ, hãy giải nén trước", "zstd is not supported, extract it first"),
    "log.why_depth": ("nén lồng nhau quá sâu", "archives nested too deeply"),
    "log.why_binary": ("không phải log đọc được (nhị phân/mã hoá/định dạng lạ)",
                       "not a readable log (binary, encrypted or unknown format)"),
    "err.no_readable_logs": ("Không có file log nào đọc được", "No readable log files"),
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
    "dr.axel": ("axel (tải tệp đính kèm nhiều kết nối)", "axel (multi-connection attachment download)"),
    "dr.axel_missing": ("Không thấy axel dùng được (app/axel-for-windows-master hoặc cấu hình) — sẽ tải bằng HTTP thường",
                      "No usable axel (app/axel-for-windows-master or Settings) — falling back to plain HTTP"),
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


LANGUAGES = {"vi": "Vietnamese", "en": "English", "ko": "Korean"}


def lang():
    value = settings.load().get("lang")
    return value if value in LANGUAGES else "vi"


def language_name(code=None):
    """English name of the UI language, for telling an AI which language to write in."""
    return LANGUAGES[code or lang()]


def _template(key, code):
    pair = MESSAGES[key]
    return KO.get(key, pair[1]) if code == "ko" else pair[1] if code == "en" else pair[0]


def t(key, **kw):
    if key not in MESSAGES:
        return key
    text = _template(key, lang())
    return text.format(**kw) if kw else text


# ---------------------------------------------------------------- re-rendering stored messages
_FIELD = re.compile(r"\{(\w+)(?::[^{}]*)?\}")
# what step cards and the assistant's scan bubbles show; log lines stay as written
RELOCALIZED = ("note.", "err.", "step.", "tz.", "asst.")


@lru_cache(maxsize=1)
def _patterns():
    """(regex, key) for every vi / en / ko template of the step-card messages, most specific first.
    Templates with fewer than 4 literal letters ("lỗi: {error}") would match almost anything and are left out."""
    found = []
    for key in MESSAGES:
        if not key.startswith(RELOCALIZED):
            continue
        for text in {_template(key, code) for code in LANGUAGES}:
            literal = _FIELD.sub("", text)
            if len(re.sub(r"[\W_]", "", literal)) < 4:
                continue
            parts, seen = [], set()
            for index, piece in enumerate(_FIELD.split(text)):
                if index % 2:  # a field name (split keeps the capture group)
                    # a value never spans the " · " that joins notes ("<title> · 18 attachment(s)")
                    parts.append(f"(?P={piece})" if piece in seen else f"(?P<{piece}>(?:(?! · ).)+?)")
                    seen.add(piece)
                elif piece:
                    parts.append(re.escape(piece))
            found.append((len(literal), re.compile("".join(parts), re.S), key))
    found.sort(key=lambda item: -item[0])
    return tuple((regex, key) for _, regex, key in found)


def _render(text, code, depth=0):
    for regex, key in _patterns():
        match = regex.fullmatch(text)
        if match:
            values = {name: (_render(value, code, depth + 1) or value) if depth < 1 else value
                      for name, value in match.groupdict().items()}
            return _FIELD.sub(lambda field: values.get(field.group(1), field.group(0)), _template(key, code))
    return None


@lru_cache(maxsize=4096)
def _relocalize(text, code):
    whole = _render(text, code)
    if whole is not None:
        return whole
    if " · " in text:  # notes are often joined: "<Jira title> · 18 attachment(s)"
        return " · ".join(_render(part, code) or part for part in text.split(" · "))
    return text


def relocalize(text, code=None):
    """`text` (a stored step note or error, in any of the three languages) in the current UI language.
    Text that is not one of our messages (an exception, a Jira title) comes back unchanged."""
    if not isinstance(text, str) or not text.strip():
        return text
    return _relocalize(text, code or lang())
