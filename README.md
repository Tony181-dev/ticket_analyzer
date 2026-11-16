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

- Thanh trên cùng: chọn **chế độ AI**, **model** (M365 mặc định **GPT-5.6 Sol Think Deeper**; Copilot CLI mặc định *auto*), ngôn ngữ **VI | EN | KO** (report trên Collab, phân tích và infographic viết theo ngôn ngữ giao diện).
- Nhập Jira key hoặc link, chọn **chế độ xử lý** rồi bấm **Chạy toàn bộ**. Ba chế độ:
  - **Tự động (Skip)** — AI đọc ticket để chọn tệp log đính kèm đáng tải (có giới hạn MB), tìm khung giờ sự cố và căn múi giờ với log; bước không làm được (không có log, tải lỗi, AI lỗi) bị **bỏ qua** và luồng chạy đến cuối với dữ liệu còn lại. Dùng cho chạy nhanh và automation.
  - **Duyệt (Review)** — luồng dừng ở bước Log: AI điền sẵn tệp đề xuất (kèm lý do), khung giờ và múi giờ; bạn sửa/xác nhận rồi bấm **Tải & chạy tiếp**, hoặc **Bỏ qua log**. Tải lỗi thì quay lại hỏi bạn thay vì chết run.
  - **Tự nhập (Manual)** — không hỏi AI: bạn tự chọn tệp cần tải, nhập khung giờ (theo giờ trong log) và độ lệch múi giờ.
- Nút **Tiếp tục** chạy tiếp từ bước đang dang dở (không làm lại bước đã xong); mỗi bước cũng có **Chạy riêng bước này** / **Chạy từ bước này** trong drawer. Các dấu **(?)** giải thích từng lựa chọn.
- **Múi giờ:** bước 2 trích giờ sự cố *đúng như ticket viết* kèm múi giờ nếu ticket nêu; trước khi cắt log, app so khung giờ với khoảng thời gian thực của từng file log (artifact `03-log-span.md`) để quyết định log in giờ gì (ví dụ ticket KST, log UTC → lệch −9h), AI chỉ phá hoà khi còn mơ hồ và bạn luôn chỉnh tay được. Khung giờ rỗng thì tự quét toàn bộ log.
  - **Ticket không nêu múi giờ:** ở chế độ Duyệt / Tự nhập, bước Log dừng sau khi log đã sẵn sàng và hỏi bạn: **theo gợi ý** (mỗi phương án cho biết khung giờ có nằm trong khoảng giờ của log không), **tự đặt** múi giờ của ticket và của log (xem trước khung giờ), hoặc **bỏ lọc theo giờ** (dùng toàn bộ log). Chế độ Tự động không dừng: căn tự động và ghi cảnh báo.
- **Trợ lý AI (bong bóng góc phải dưới):** chạy bằng Copilot CLI với model trong Cấu hình, nhìn thấy trạng thái hệ thống (ticket đang mở, các bước, lỗi, tệp đính kèm, log, cấu hình). Hỏi bất cứ gì hoặc bấm **Quét**; trợ lý gợi ý tải/bỏ tệp nào, khung giờ & múi giờ, nguồn code liên quan, cấu hình cần chỉnh, và tạo **bug ticket Markdown** cho lỗi của chính tool (`data/bugs/`). Mọi đề xuất hiện thành nút **Thực hiện** — không gì thay đổi khi bạn chưa bấm. Khi luồng dừng chờ chọn bằng chứng / múi giờ hoặc một bước lỗi, trợ lý tự quét một lần (tắt được trong Cấu hình).
- Bước đang chạy có bánh răng quay. Bấm vào một bước để xem file kết quả: brief log, catalog, sơ đồ code graph tương tác,
  phân tích, bản xem trước report. Có các nút Copy / Tải về / Mở tab mới / Mở thư mục.
- **Tệp đính kèm** (trong bước Jira): chọn file cần **Tải** và file cần **Phân tích**, chọn thư mục tải
  (để trống thì dùng `Downloads\TicketAnalyzer\<ISSUE>`). File đã tải đủ dung lượng thì không tải lại.
  Tiến độ hiện **từng tệp một thanh** (chờ / đang tải % / đã tải / đã có sẵn / lỗi / đang giải nén / đã giải nén).
  File nén (zip/tar/7z/rar) được giải nén; các file log bên trong được đưa vào bước Log brief.
- **AI hiểu ticket** chỉ đọc Jira và trả timestamp chuẩn hóa `HH:MM:SS[.mmm]` trong JSON. Thiếu/sai timestamp: Auto match toàn catalog trên toàn khoảng log.
- **LogSleuth Auto / Manual:** Auto tự scan source và slice theo timestamp bước trước với cấu hình mặc định. Manual dừng ở bước Log brief để thao tác **Scan → chọn catalog/service → chọn khung giờ/tuning → Slice**; xem brief rồi bấm **Proceed** để sang bước tiếp theo. Xem [hướng dẫn tích hợp](docs/logsleuth-integration.md).
- **Token cap:** toàn bộ log brief (kể cả bảng/heading) có trần 50.000, có thể giảm ở Settings hoặc từng run. Dùng cận trên bảo thủ theo UTF-8 byte, không chỉ ước lượng `chars/4`. Ticket/graph/lịch sử AI vẫn dùng context riêng, nên giảm ngân sách nếu context tổng lớn.
- **AI phân tích** dùng high reasoning/long context với Copilot CLI. Khi LogSleuth match được câu lệnh log trong source, app gửi thêm đoạn code quanh chính các `file:line` đó; graph chỉ là chỉ mục cấu trúc, không được coi là bằng chứng hành vi của hàm. Phân tích có mục **Suggested Solution and Code Location**: source có liên quan không, vị trí nghi ngờ (`file:line`, symbol, độ tin cậy), hướng sửa và phạm vi ảnh hưởng.
- **Step 07 · AI infographic** bật/tắt được, dùng với cả M365 và Copilot CLI. M365 tạo ảnh; nếu M365 lỗi (giới hạn tạo ảnh trong ngày, từ chối, timeout) app tự **fallback** dựng SVG cục bộ từ nội dung phân tích. Copilot CLI luôn dùng đường SVG (theo [instructions/infographic.md](instructions/infographic.md): mức độ, độ tin cậy, số liệu chính, dòng thời gian, bằng chứng, vị trí trong code, hành động — layout tự giãn theo nội dung). Ảnh chỉ là trang trí: publish **không bao giờ** bị chặn vì thiếu ảnh — trang vẫn đăng, ghi chú nêu rõ thiếu infographic, và chạy lại bước 7 rồi publish sẽ đính ảnh vào đúng trang đó.
- **Token & thời gian** (tab cạnh Nhật ký): mỗi lần bấm Chạy là một **lượt**; chạy lại ticket cộng thêm lượt mới chứ không ghi đè, tổng token = cộng mọi lượt. Copilot CLI đo thật; M365 không báo usage nên hiển thị **ước lượng** (byte/4, đánh dấu ~).
- **Automation:** `python -m app run ABC-1234 --publish --json` (hoặc `TicketAnalyzer.exe run …`) chạy không cần giao diện; REST API điều khiển app đang chạy. Chi tiết: [docs/automation.md](docs/automation.md).
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
