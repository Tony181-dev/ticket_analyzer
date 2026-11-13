# Ticket Analyzer

Tác giả: **hoang6.nguyen**

Phân tích Jira ticket bằng AI rồi đăng report lên Collab, chạy trên localhost với giao diện web (Tiếng Việt / English).
Gộp 3 extension cũ (Issue-Analyzer / local proxy M365, LogSleuth, CodeGraph) thành một app.

```
[1 Jira] → [2 AI hiểu ticket] → [3 Log brief] → [4 Code graph] → [5 AI phân tích] → [6 AI format] → [7 AI infographic] → [8 Đăng Collab]
  MCP        tìm timestamp ticket  LogSleuth        tuỳ chọn         M365 | CLI         Confluence XHTML   tuỳ chọn              create / update
```

Bước 2–3 chạy khi có log (chọn tay hoặc lấy từ attachment). Có Source code folder thì LogSleuth đối chiếu log với catalog câu lệnh trong source; nếu không có, nó vẫn tạo brief bằng từ khóa ticket và tín hiệu lỗi phổ biến. Bước 4 chạy riêng khi có source folder và bật **Dùng Code Graph**.

## Cài & chạy

**Dùng bản đóng gói (không cần Python):** giải nén `TicketAnalyzer-<version>-win64.zip`, chạy `TicketAnalyzer\TicketAnalyzer.exe`.

**Chạy từ mã nguồn:**

1. Cài **Python 3.10+** (tick *Add python.exe to PATH*).
2. Chạy **`start.bat`** (macOS/Linux: `./start.sh`), trình duyệt tự mở `http://localhost:6969`.
   `.venv` dùng lại các gói Python đã có trên máy (Playwright, Chromium, uv), **không cài lại** thứ đã có.
   Nếu thư viện đã đủ, script không gọi pip.
3. Bấm nút trạng thái ở góc phải → **Tự sửa tất cả**. App chỉ cài những gì còn thiếu (Node.js, uv, Playwright/Chromium,
   Copilot CLI tuỳ chế độ). Những việc chỉ người dùng làm được:
   - **Token Jira / Collab**: tự lấy từ `mcp.json` của VS Code (`mcp_jira`/`mcp_collab`). Nếu chưa có thì nhập 2 token vào form.
   - **Đăng nhập**: M365 (mở Chromium) hoặc Copilot CLI (`copilot login`). Nếu đã `gh auth login` thì app nhận luôn.
     Nút **Kiểm tra thật** gửi một prompt rất ngắn để xác nhận.

  App tự kiểm tra phiên đăng nhập M365 một lần ở nền khi khởi động (nếu Playwright/Chromium sẵn sàng); ở chế độ Copilot CLI, kết quả này chỉ mang tính tuỳ chọn.

Mạng công ty chặn SSL: `start.bat` và Setup tự xử lý bằng chứng chỉ của Windows, rồi dùng `--trusted-host` nếu vẫn lỗi.

## Dùng

- Thanh trên cùng: chọn **chế độ AI**, **model** (M365 mặc định **GPT-5.6 Sol Think Deeper**; Copilot CLI mặc định *auto*), ngôn ngữ **VI | EN**.
- Nhập Jira key hoặc link. Chọn file log và source code folder ở giao diện chính; **Auto** dùng các đầu vào này cùng thông số mặc định, không yêu cầu tinh chỉnh trong step. Chọn **Manual** nếu muốn dừng ở Log brief, tự Scan/Slice, xem kết quả rồi bấm **Proceed** để chạy tiếp. Tùy chọn **Tiếp tục chạy nếu step 2, 3, 4 lỗi** cho phép workflow chạy tiếp khi AI hiểu ticket, Log brief hoặc Code Graph lỗi; các bước sau chỉ dùng evidence đã tạo thành công. Các dấu **(?)** giải thích từng lựa chọn.
- Nếu bật **Tiếp tục chạy nếu step 2, 3, 4 lỗi**, pipeline vẫn ghi nhận lỗi nhưng tiếp tục qua bước sau; phân tích chỉ nhận artifact của các bước thành công. Lỗi ở bước Jira, phân tích, format hoặc Collab vẫn dừng pipeline.
- Bước đang chạy có bánh răng quay. Bấm vào một bước để xem file kết quả: brief log, catalog, sơ đồ code graph tương tác,
  phân tích, bản xem trước report. Có các nút Copy / Tải về / Mở tab mới / Mở thư mục.
- **Tệp đính kèm** (trong bước Jira): chọn file cần **Tải** và file cần **Phân tích**, chọn thư mục tải
  (để trống thì dùng `Downloads\TicketAnalyzer\<ISSUE>`). File đã tải đủ dung lượng thì không tải lại.
  File nén (zip/tar/7z/rar) được giải nén; các file log bên trong được đưa vào bước Log brief.
- **AI hiểu ticket** chỉ đọc Jira và trả timestamp chuẩn hóa `HH:MM:SS[.mmm]` trong JSON. Thiếu/sai timestamp: Auto match toàn catalog trên toàn khoảng log.
- **LogSleuth Auto / Manual:** Auto tự scan source và slice theo timestamp bước trước với cấu hình mặc định. Manual dừng ở bước Log brief để thao tác **Scan → chọn catalog/service → chọn khung giờ/tuning → Slice**; xem brief rồi bấm **Proceed** để sang bước tiếp theo. Xem [hướng dẫn tích hợp](docs/logsleuth-integration.md).
- **Token cap:** toàn bộ log brief (kể cả bảng/heading) có trần 50.000, có thể giảm ở Settings hoặc từng run. Dùng cận trên bảo thủ theo UTF-8 byte, không chỉ ước lượng `chars/4`. Ticket/graph/lịch sử AI vẫn dùng context riêng, nên giảm ngân sách nếu context tổng lớn.
- **AI phân tích** dùng high reasoning/long context với Copilot CLI. Khi LogSleuth match được câu lệnh log trong source, app gửi thêm đoạn code quanh chính các `file:line` đó; graph chỉ là chỉ mục cấu trúc, không được coi là bằng chứng hành vi của hàm.
- **Step 07 · AI infographic** có toggle **Auto / Skip** ngay trong thẻ bước, dùng được với cả M365 và Copilot CLI. M365 tạo ảnh; Copilot CLI trả nội dung có cấu trúc để app dựng SVG cục bộ. Auto sẽ chạy bước khi pipeline đi đến đây; Skip bỏ qua bước và report vẫn publish bình thường. Nếu Auto mà tạo/đính ảnh lỗi, publish sẽ báo lỗi thay vì hoàn tất thiếu infographic.
- Source code folder được dùng chung để tạo catalog cho LogSleuth và (nếu bật) chạy Code Graph. Code Graph có checkbox riêng nên có thể tắt mà vẫn giữ catalog cho Log brief; nếu không chọn source folder, LogSleuth dùng chế độ tìm log dự phòng.
- Sửa được kết quả **AI phân tích** và **XHTML report**. Sau khi lưu, các bước sau tự đánh dấu *Cần chạy lại*.
- Mặc định app dừng trước bước 8 để duyệt rồi mới **Đăng lên Collab**. Nếu trang trùng tên đã tồn tại thì app cập nhật trang đó.

## Hai chế độ AI

| | M365 Copilot | Copilot CLI |
|---|---|---|
| Cách gọi | Playwright điều khiển m365.cloud.microsoft/chat, đính kèm file | `copilot -p … -s`, chỉ cấp quyền đọc file trong thư mục run |
| Model | Chọn theo tên hiển thị trong M365 (mặc định GPT-5.6 Sol Think Deeper) | `--model`, để trống = auto |
| Infographic | Ảnh do M365 Copilot tạo | Nội dung tóm tắt do CLI tạo, render thành SVG cục bộ |
| Đăng nhập | Nút “Đăng nhập M365” | `copilot login` hoặc `gh auth login` |

## Bộ cài (Setup) và gỡ cài đặt

```
build_installer.bat            # build EXE rồi tạo bộ cài
build_installer.bat --skip-build   # dùng lại dist\TicketAnalyzer đã build
```

Kết quả: `dist\TicketAnalyzer-Setup-<version>.exe` (Inno Setup; script tự cài Inno Setup qua winget nếu thiếu).
- Cài theo user, **không cần admin**, vào `%LOCALAPPDATA%\Programs\TicketAnalyzer`. Tạo shortcut Start Menu, tuỳ chọn Desktop.
- Dữ liệu lưu ở `%LOCALAPPDATA%\TicketAnalyzer`; cài bản mới đè lên không mất lịch sử/token.
- **Gỡ cài đặt**: Settings → Apps → Ticket Analyzer, hoặc Start Menu → *Uninstall Ticket Analyzer*.
  Trình gỡ tự tắt app đang chạy và hỏi có xoá luôn dữ liệu (lịch sử, cấu hình, token) hay không.

## Đóng gói EXE để chia sẻ

```
build_exe.bat
```

Script cài PyInstaller (chỉ dùng khi build) vào `.venv`, đóng gói app kèm Playwright và uv, rồi tạo
`dist\TicketAnalyzer-<version>-win64.zip`. Người nhận chỉ cần giải nén và chạy `TicketAnalyzer.exe`.
Dữ liệu được lưu ở `data\` cạnh file exe; nếu thư mục đó không ghi được thì lưu ở `%LOCALAPPDATA%\TicketAnalyzer`.
Máy người nhận vẫn cần Node.js (cho LogSleuth / Copilot CLI); Setup tự cài nếu thiếu.

## Cấu hình & dữ liệu

- `data/config/settings.json`: ngôn ngữ, model, space key, parent page, thư mục tải… (sửa qua ⚙ trong app).
- `data/config/mcp.json`: cấu hình MCP, **chứa token**, đã nằm trong `.gitignore`.
- `data/runs/<ISSUE>-<thời gian>/`: toàn bộ file của mỗi lần chạy (`01-jira.md` … `07-collab.json`, `run.log`).
- `instructions/*.md`: prompt cho từng bước AI, sửa trực tiếp được.

## Phát triển

```
python -m unittest discover -s tests     # pipeline, đính kèm, cài đặt (MCP/AI/Jira giả lập)
python -m app                            # chạy server (TA_PORT, TA_DATA_DIR, TA_NO_BROWSER)
```

Tổng quan hệ thống: [docs/system-overview.md](docs/system-overview.md). Thiết kế chi tiết: [docs/design.md](docs/design.md).
