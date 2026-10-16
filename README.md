# Ticket Analyzer

Phân tích Jira ticket bằng AI rồi đăng report lên Collab, chạy trên localhost với giao diện web.
Gộp 3 extension cũ (Issue-Analyzer / local proxy M365, LogSleuth, CodeGraph) thành một app.

```
[1 Jira] → [2 AI hiểu ticket] → [3 Log brief] → [4 Code graph] → [5 AI phân tích] → [6 AI format] → [7 Đăng Collab]
  MCP        chọn khung giờ       LogSleuth        CodeGraph        M365 | CLI         Confluence XHTML   create / update
```

Bước 2–4 là tuỳ chọn: chỉ chạy khi có file log hoặc folder source.

## Cài & chạy

1. Cài **Python 3.10+** (tick *Add python.exe to PATH*).
2. Chạy **`start.bat`** (macOS/Linux: `./start.sh`). Lần đầu script tự tạo `.venv` và cài thư viện, sau đó mở `http://localhost:6969`.
3. Bấm nút trạng thái ở góc phải → **Tự sửa tất cả**. App tự cài Node.js, uv, Playwright/Chromium, Copilot CLI (tuỳ chế độ).
   Những việc chỉ người dùng làm được:
   - **Token Jira / Collab**: app tự lấy từ `mcp.json` của VS Code nếu đã có `mcp_jira`/`mcp_collab`. Nếu chưa có, nhập 2 token vào form.
   - **Đăng nhập**: M365 (mở Chromium để đăng nhập) hoặc Copilot CLI (`copilot login`).

## Dùng

- Nhập Jira key hoặc link. Có thể chọn thêm file log và/hoặc folder source, rồi bấm **Chạy toàn bộ**.
- Bước đang chạy có bánh răng quay. Bấm vào một bước để xem file kết quả: brief log, catalog,
  sơ đồ code graph tương tác, phân tích, bản xem trước report. Có các nút Copy / Tải về / Mở tab mới / Mở thư mục.
- **AI hiểu ticket** đọc ticket cùng dải giờ thực tế trong log, rồi chọn khung giờ xảy ra issue và các từ khoá.
  LogSleuth chỉ giữ log trong khung đó. Bạn có thể sửa khung giờ trong bước 3 rồi bấm **Cắt lại log**.
- Sửa được kết quả **AI phân tích** và **XHTML report**. Sau khi lưu, các bước sau tự đánh dấu *Cần chạy lại*.
- Mặc định app dừng ở bước 7 để bạn duyệt report, sau đó bấm **Đăng lên Collab**. Nếu trang trùng tên đã tồn tại thì app cập nhật trang đó.

## Hai chế độ AI

| | M365 Copilot | Copilot CLI |
|---|---|---|
| Cách gọi | Playwright điều khiển m365.cloud.microsoft/chat, đính kèm file | `copilot -p … -s`, chỉ cấp quyền đọc file trong thư mục run |
| Đăng nhập | Nút “Đăng nhập M365” | Nút “Đăng nhập Copilot” |
| Ngữ cảnh | Các bước dùng chung một hội thoại | Mỗi bước một lần gọi, đọc lại file |

## Cấu hình & dữ liệu

- `data/config/settings.json`: space key, parent page, model… (sửa qua nút ⚙ trong app).
- `data/config/mcp.json`: cấu hình MCP, **chứa token**, đã nằm trong `.gitignore`.
- `data/runs/<ISSUE>-<thời gian>/`: toàn bộ file của mỗi lần chạy (`01-jira.md` … `07-collab.json`, `run.log`).
- `instructions/*.md`: prompt cho từng bước AI, sửa trực tiếp được.

## Phát triển

```
python -m unittest discover -s tests     # pipeline end-to-end với MCP/AI giả lập
python -m app                            # chạy server (TA_PORT, TA_DATA_DIR, TA_NO_BROWSER)
```

Thiết kế chi tiết: [docs/design.md](docs/design.md).
