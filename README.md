# Ticket Analyzer

Tác giả: **hoang6.nguyen**

Phân tích Jira ticket bằng AI rồi đăng report lên Collab, chạy trên localhost với giao diện web (Tiếng Việt / English).
Gộp 3 extension cũ (Issue-Analyzer / local proxy M365, LogSleuth, CodeGraph) thành một app.

```
[1 Jira] → [2 AI hiểu ticket] → [3 Log brief] → [4 Code graph] → [5 AI phân tích] → [6 AI format] → [7 Đăng Collab]
  MCP        chọn khung giờ       LogSleuth        CodeGraph        M365 | CLI         Confluence XHTML   create / update
```

Bước 2–4 là tuỳ chọn: chỉ chạy khi có file log (chọn tay hoặc lấy từ đính kèm của ticket) hoặc folder source.

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

Mạng công ty chặn SSL: `start.bat` và Setup tự xử lý bằng chứng chỉ của Windows, rồi dùng `--trusted-host` nếu vẫn lỗi.

## Dùng

- Thanh trên cùng: chọn **chế độ AI**, **model** (M365 mặc định **GPT 6 Luna**; Copilot CLI mặc định *auto*), ngôn ngữ **VI | EN**.
- Nhập Jira key hoặc link. Có thể chọn thêm file log và/hoặc folder source. Tick **Tự tải & phân tích log/zip đính kèm**
  nếu muốn lấy log từ ticket. Sau đó bấm **Chạy toàn bộ**.
- Bước đang chạy có bánh răng quay. Bấm vào một bước để xem file kết quả: brief log, catalog, sơ đồ code graph tương tác,
  phân tích, bản xem trước report. Có các nút Copy / Tải về / Mở tab mới / Mở thư mục.
- **Tệp đính kèm** (trong bước Jira): chọn file cần **Tải** và file cần **Phân tích**, chọn thư mục tải
  (để trống thì dùng `Downloads\TicketAnalyzer\<ISSUE>`). File đã tải đủ dung lượng thì không tải lại.
  File nén (zip/tar/7z/rar) được giải nén; các file log bên trong được đưa vào bước Log brief.
- **AI hiểu ticket** đọc ticket cùng dải giờ thực tế trong log, rồi chọn khung giờ xảy ra issue và các từ khoá.
  Bạn có thể sửa khung giờ trong bước 3 rồi bấm **Cắt lại log**.
- Sửa được kết quả **AI phân tích** và **XHTML report**. Sau khi lưu, các bước sau tự đánh dấu *Cần chạy lại*.
- Mặc định app dừng ở bước 7 để duyệt rồi mới **Đăng lên Collab**. Nếu trang trùng tên đã tồn tại thì app cập nhật trang đó.

## Hai chế độ AI

| | M365 Copilot | Copilot CLI |
|---|---|---|
| Cách gọi | Playwright điều khiển m365.cloud.microsoft/chat, đính kèm file | `copilot -p … -s`, chỉ cấp quyền đọc file trong thư mục run |
| Model | Chọn theo tên hiển thị trong M365 (mặc định GPT 6 Luna) | `--model`, để trống = auto |
| Đăng nhập | Nút “Đăng nhập M365” | `copilot login` hoặc `gh auth login` |

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

Thiết kế chi tiết: [docs/design.md](docs/design.md).
