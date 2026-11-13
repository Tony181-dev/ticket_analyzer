# Tổng quan hệ thống Ticket Analyzer

## Mục tiêu

Ticket Analyzer là ứng dụng web chạy cục bộ để gom dữ liệu từ Jira, log và source code; dùng AI tạo phân tích kỹ thuật; chuyển phân tích thành báo cáo Confluence Storage XHTML; sau khi người dùng duyệt thì đăng/cập nhật trang trên Collab. Ứng dụng hợp nhất Jira/M365, LogSleuth và CodeGraph thành một quy trình có artifact và lịch sử riêng cho từng lần chạy.

## Kiến trúc

```mermaid
flowchart LR
    UI[Trình duyệt: web/index.html] -->|HTTP và SSE| Server[app/server.py<br/>localhost]
    Server --> Executor[app/pipeline.py<br/>Executor tuần tự, có thể dừng]
    Executor -->|stdio JSON-RPC| MCP[MCP Jira / Confluence]
    Executor --> AI[app/ai<br/>M365 Playwright hoặc Copilot CLI]
    Executor --> Prep[app/logprep.py<br/>Chuẩn hóa log và archive]
    Prep --> LS[LogSleuth<br/>catalog và cắt log]
    Executor --> CG[CodeGraph<br/>đồ thị cấu trúc source]
    Executor --> Report[Chuẩn hóa / refine XHTML]
    Report -->|XHTML| Executor
    Executor --> Runs[data/runs/ISSUE-thời_gian]
    Runs -->|artifact qua API| Server
    Server -->|JSON, file và SSE| UI
```

- **Giao diện và API:** giao diện tĩnh nằm ở [web/index.html](../web/index.html). [app/server.py](../app/server.py) dùng `ThreadingHTTPServer` của Python, chỉ bind `127.0.0.1`; cập nhật trạng thái/log qua Server-Sent Events.
- **Điều phối:** [app/pipeline.py](../app/pipeline.py) quản lý trạng thái từng bước, điều kiện chạy, hủy tác vụ, artifact và yêu cầu duyệt trước khi publish. Hiện chỉ có một `Executor` chạy tại một thời điểm vì worker M365 dùng chung một phiên trình duyệt.
- **Tích hợp:** [app/mcp.py](../app/mcp.py) gọi Jira/Confluence qua MCP stdio; [app/ai/__init__.py](../app/ai/__init__.py) điều phối hai backend: M365 Copilot được điều khiển bằng Playwright hoặc GitHub Copilot CLI.
- **Dữ liệu:** mỗi lần chạy lưu trong `data/runs/<ISSUE>-<timestamp>/`, gồm file kết quả đánh số bước, `run.json` (trạng thái/đầu vào), và `run.log`. Cấu hình thường ở `data/config/settings.json`; token MCP ở `data/config/mcp.json`.

## Luồng xử lý

| Bước | Tác vụ | Điều kiện | Kết quả / vai trò |
|---|---|---|---|
| 1. Jira | Lấy ticket và attachment metadata qua MCP | Luôn chạy | `01-jira.md`, JSON thô và danh sách attachment |
| 2. Hiểu ticket | AI trích xuất giờ xảy ra sự cố được nêu rõ trong ticket | Có log | `02-understanding.md`, `02-focus.json`; không đọc log hay source ở bước này |
| 3. Log brief | Chuẩn hóa log rồi dùng LogSleuth lọc tín hiệu, gom ngữ cảnh, giảm lặp và giới hạn dung lượng | Có log | `03-log-brief.md`; nếu có source thì thêm `03-catalog.md` và đối chiếu các câu lệnh log trong code; nếu không có source thì dùng từ khóa ticket và tín hiệu lỗi phổ biến |
| 4. Code graph | Tạo đồ thị file/symbol/quan hệ và bản tóm tắt cho AI | Có source và bật Code Graph | HTML tương tác, JSON đầy đủ/thu gọn, report |
| 5. Phân tích | AI tổng hợp ticket và các artifact đang hợp lệ | Luôn chạy | `05-analysis.md`; khi LogSleuth chỉ ra source location, đính kèm thêm đoạn code giới hạn quanh các dòng đó |
| 6. Định dạng | AI đổi phân tích thành Confluence Storage XHTML; sửa cục bộ trước, chỉ gọi AI refine theo đoạn nếu còn lỗi | Luôn chạy | `06-report.xhtml`, bản preview, phản hồi thô và log refine |
| 7. Infographic | Toggle Auto/Skip ngay tại Step 07; M365 tạo ảnh, Copilot CLI trả nội dung JSON để dựng SVG an toàn cục bộ | Auto chạy cùng pipeline; Skip bỏ qua. Khi Auto, lỗi tạo/đính ảnh chặn publish | `06-infographic.*`, đính kèm và nhúng vào trang Collab |
| 8. Publish | Tạo trang hoặc cập nhật trang cùng tiêu đề nếu đã tồn tại | Sau bước duyệt; nếu infographic bật, chỉ hoàn tất sau khi ảnh được đính và trang được cập nhật | `07-collab.json` và URL trang |

### Hai đường xử lý log

LogSleuth có hai chế độ: **Auto** dùng log/source đã chọn ở giao diện chính, tự scan và slice theo
timestamp chuẩn hóa của bước 2 với cấu hình mặc định. **Manual** dừng pipeline để thao tác Scan →
chọn catalog/service → chọn khung giờ/tuning → Slice; người dùng xem brief rồi bấm Proceed để chạy
tiếp. Thiếu/sai timestamp trong Auto thì dùng toàn catalog trên toàn khoảng log.
Toàn bộ Markdown đầu ra có cap bảo thủ 2.000–50.000 token, tính cả heading/bảng theo byte UTF-8;
ngân sách per-run không vượt Settings. Chi tiết: [docs/logsleuth-integration.md](logsleuth-integration.md).

1. Có source folder: [app/evidence/logsleuth.py](../app/evidence/logsleuth.py) gọi [logsleuth/cli.js](../logsleuth/cli.js) để quét template log trong source, rồi khớp các anchor với log runtime. [app/logprep.py](../app/logprep.py) hỗ trợ archive lồng nhau, nén, encoding phổ biến, JSON array và DLT.
2. Không có source folder: LogSleuth vẫn tạo brief bằng từ khóa ticket và các tín hiệu như error/failure/timeout/crash. Kết quả có thể hữu ích để khoanh vùng nhưng không cung cấp liên kết `file:line` từ source.

### Diễn giải bằng chứng

- LogSleuth chứng minh một template/log pattern xuất hiện trong log; riêng việc khớp log không chứng minh nguyên nhân.
- CodeGraph là chỉ mục cấu trúc, không phải call trace hay bằng chứng hành vi runtime. Bước phân tích chỉ nên khẳng định hành vi khi có đoạn source evidence hoặc log/ticket hỗ trợ.
- Cửa sổ thời gian hiện dùng giờ trong ngày; ticket không nêu giờ thì pipeline không lọc theo thời gian. Nếu lọc ra 0 dòng, bước log thử lại trên toàn bộ khoảng log.

## Cấu hình và vận hành

- Chọn backend AI, model, ngôn ngữ, thư mục tải attachment, ngân sách LogSleuth/CodeGraph và Collab space qua UI/cấu hình.
- Tùy chọn ở dưới Jira cho phép tiếp tục sau lỗi ở bước 2–4; step lỗi vẫn được ghi nhận, và AI chỉ nhận evidence từ các step thành công. Lỗi ở bước bắt buộc vẫn dừng pipeline.
- Ứng dụng kiểm tra phiên đăng nhập M365 một lần ở nền khi khởi động nếu Playwright/Chromium sẵn sàng; trạng thái này là tuỳ chọn khi đang dùng Copilot CLI.
- Attachment được tải có chọn lọc; archive được giải nén với kiểm tra path traversal và giới hạn dung lượng.
- Copilot CLI chạy trong thư mục artifact của run, không bật shell/write tool và tắt MCP tích hợp sẵn. Token không được gửi xuống trình duyệt.
- Báo cáo preview được xử lý cục bộ; PlantUML không được gửi tới renderer bên ngoài.
- [README.md](../README.md) mô tả cách cài, chạy, đóng gói và sử dụng.

## Các ưu tiên để nâng chất lượng pipeline

1. **Độ mới của cache:** catalog và CodeGraph hiện lưu fingerprint metadata source (đường dẫn tương đối, kích thước, `mtime_ns`); graph thu gọn cũng được tạo lại khi ngân sách token đổi. Nếu cần bắt cả trường hợp nội dung đổi nhưng kích thước/mtime bị giữ nguyên, có thể nâng cấp manifest sang content hash hoặc watcher.
2. **Dependency và trạng thái stale:** khi chạy lại từ một bước giữa, pipeline hiện tự lùi để chạy lại prerequisite stale và chỉ truyền artifact đã `done` cho bước phân tích. Về lâu dài nên khai báo dependency thành DAG thay vì dựa vào thứ tự tuyến tính của các bước.
3. **Ngân sách ngữ cảnh tổng:** log brief và graph có ngân sách riêng; nên áp ngân sách cho tổng ticket + log + graph + source excerpt trước khi gửi AI để giảm lỗi vượt context và chi phí.
4. **Khả năng phục hồi:** bổ sung retry/backoff có giới hạn cho lỗi tạm thời ở AI/MCP, phân biệt lỗi có thể retry với publish không an toàn, và lưu metadata về model/prompt/input fingerprint cho từng bước.
5. **Thời gian log:** nếu log có nhiều ngày hoặc nhiều múi giờ, hỗ trợ ngày/offset sẽ chính xác hơn chỉ lọc theo giờ trong ngày.

Pipeline hiện đã có fallback log khi thiếu source, refresh cache theo fingerprint metadata và chạy lại prerequisite stale. Ưu tiên tiếp theo: ngân sách context tổng và retry/backoff an toàn; sau đó khai báo DAG rõ hơn, hỗ trợ ngày/múi giờ cho log và tối ưu thời gian chạy/UI.
