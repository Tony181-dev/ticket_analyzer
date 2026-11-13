# LogSleuth: Auto / Manual và giới hạn đầu ra

## Nguồn tích hợp

Core LogSleuth 1.4.2 từ dự án `0_logsleuth` được nhúng trong ứng dụng, không cần mở VS Code hay cài extension. Scanner, catalog và thuật toán slice của tool gốc được giữ nguyên. Bridge [logsleuth/cli.js](../logsleuth/cli.js) nối core Node với [app/evidence/logsleuth.py](../app/evidence/logsleuth.py). [logsleuth/src/budget.js](../logsleuth/src/budget.js) thêm kiểm tra giới hạn **sau khi render** để tính cả Markdown.

Không phụ thuộc đường dẫn tuyệt đối của tool gốc khi chạy hoặc đóng gói. Parser hiện có của ứng dụng được giữ lại, không ghi đè các thay đổi parser đã có.

## Auto (mặc định)

1. Chọn các file log và source root ở giao diện chính; chọn **Auto** trên ô LogSleuth.
2. Bước 2 chỉ đọc ticket, trả JSON timestamp. Server kiểm tra và ghi hợp đồng chuẩn hóa.
3. Bước Log brief tự scan toàn bộ source root theo các giới hạn/exclude của scanner. Catalog cache được làm mới khi metadata source hoặc tham số scan thay đổi.
4. Match log với **tất cả service trong catalog**, dùng cửa sổ timestamp hợp lệ. Không tự thu hẹp service theo tên AI đoán.
5. Nếu artifact timestamp thiếu, JSON hỏng hoặc một thời điểm không hợp lệ: match toàn bộ catalog trên **toàn khoảng thời gian log**. Không áp bộ lọc keyword AI trong nhánh này.
6. Nếu cửa sổ hợp lệ nhưng không có record nào nằm trong cửa sổ: Auto thử lại toàn khoảng log và ghi `fullTimeFallback`. Không nới cửa sổ chỉ vì budget nhỏ hoặc source không match.
7. Nếu không chọn source, vẫn có nhánh tìm keyword/tín hiệu lỗi phổ biến; không có bằng chứng nối với source.

“Full source” nghĩa là mọi file source scanner hỗ trợ trong source root đã chọn, sau các exclude/giới hạn kích thước; không phải gửi toàn bộ source/raw log cho AI. Auto dùng thông số mặc định LogSleuth và cap token toàn ứng dụng, không cần chỉnh catalog/service/tuning ở step. Kết quả vẫn bị giới hạn token và giảm lặp.

## Manual: cùng quy trình Scan → Slice của tool gốc

Chọn **Manual** ở giao diện chính; pipeline dừng tại bước Log brief trước khi gửi phân tích cho AI. Mở thẻ Log brief:

1. Chọn source root → **Scan source → catalog**. Scan có thể chạy riêng, không gọi Jira/AI và không cần log để tạo catalog.
2. Hoặc chọn catalog đã có trong cache, browse catalog Markdown/JSON, hay nhập đường dẫn rồi bấm **Nạp**. Catalog có machine-readable payload như tool gốc.
3. Chọn service từ catalog (không chọn = tất cả). Thêm/bỏ từng log file.
4. Bật/tắt time window; nhập `HH:MM`, `HH:MM:SS` hoặc `HH:MM:SS.mmm`. Cho phép mở một đầu hoặc qua nửa đêm. Manual dùng thời gian người chọn, không ghi đè bằng AI và **không tự nới cửa sổ rỗng**.
5. Tuning gồm token budget, before/after, cửa sổ PID/TID theo ms, max repeat, min level, noise floor, parser, anchor length, max source-file MB, regex thêm/loại bỏ và exclude directory.
6. Bấm **Slice → log brief** để tạo/cập nhật brief và xem kết quả. Khi đã hài lòng, bấm **Proceed** để chạy các bước tiếp theo. **Lưu** chỉ lưu tùy chọn, không gọi AI.

Nếu service không có trong catalog, báo lỗi thay vì tự mở rộng sang service khác. Nếu catalog thuộc checkout khác source folder hiện chọn, không dùng catalog đó để trích source evidence từ sai checkout.

## Hợp đồng timestamp của bước 2

Artifact chuẩn hóa có dạng:

```json
{
  "schema_version": 1,
  "time_basis": "log-local-clock",
  "time_start": "12:28:00",
  "time_end": "12:31:00.250",
  "filter_mode": "window",
  "confidence": "high",
  "reason": "Ticket nêu incident lúc 12:30.",
  "keywords": ["timeout"],
  "services": []
}
```

Không có thời điểm dùng được: hai trường thời gian là JSON `null`, `filter_mode` là `full`. Server zero-pad giờ, bổ sung giây cho `HH:MM`, chuẩn hóa phần lẻ thành 3 chữ số và kiểm tra giờ 0–23/phút-giây 0–59. Nếu một đầu được cung cấp nhưng sai, bỏ **cả cửa sổ** để tránh vô tình lọc quá hẹp.

`time_basis` chỉ đồng hồ trong log; không tự chuyển múi giờ và không suy thời gian từ ngày tạo/cập nhật Jira. Lọc hiện vẫn theo giờ trong ngày như tool gốc; log nhiều ngày cùng giờ cần bước nâng cấp date-aware riêng.

## Token cap cho GPT 6 Luna

- Mặc định và trần cứng: **50.000**; có thể chọn 2.000–50.000. Cap per-run không được vượt cap toàn ứng dụng trong Settings.
- Thuật toán gốc vẫn dùng ước lượng `chars / 4` để chọn seed/context. Nhưng ước lượng đó **không đủ** để cam kết giới hạn file Markdown cuối cùng.
- Guard cuối dùng **số byte UTF-8** làm cận trên bảo thủ cho tokenizer dựa trên byte. Tính cả heading, bảng, tên file, code fence, log và ghi chú. Đây không phải số token chính xác của GPT 6 Luna.
- Nếu vượt cap: giữ vùng điểm cao hơn, bỏ vùng ít ưu tiên; chia chunk lớn ở ranh giới record, không cắt giữa dòng hoặc tách continuation chain khỏi dòng cha. Một record/stack trace quá lớn để vừa cap có thể bị bỏ toàn bộ. Phần metadata rất lớn được chuyển sang summary gọn.
- Python kiểm tra lại kích thước file trước khi chuyển artifact cho bước AI. File vượt giới hạn không được chấp nhận.
- Thống kê tách `tokenEstimate` (ước lượng), `tokenUpperBound`/`tokens` (cận trên), `budgetTokens`, `outputBytes`, `outputTruncated`, `compactMetadata`.

**Giới hạn cam kết:** log brief luôn nằm trong cap nêu trên. Khả năng model đọc được *toàn bộ request* còn phụ thuộc ticket, source excerpt, graph, system prompt và lịch sử hội thoại; không thể khẳng định context thật của mọi phiên GPT 6 Luna từ tên model. Nếu context tổng lớn, giảm budget log/graph tương ứng.

## Artifact và API

- Catalog snapshot của run, log brief Markdown và thống kê JSON nằm trong thư mục artifact, được hiển thị ở bước Log brief. Stats lưu requested focus, mode và fallback thực tế.
- `POST /api/runs` và `POST /api/runs/<id>/inputs`: trường `logsleuth` chứa tùy chọn per-run.
- `POST /api/runs/<id>/log-scan`: scan catalog riêng, có thể dừng bằng nút Stop.
- `GET /api/logsleuth/catalogs`: các catalog cache gần nhất.
- `POST /api/logsleuth/catalog-info` với `{ "path": "..." }`: thống kê/service của catalog được chọn.
- `POST /api/runs/<id>/start` với `{ "from": "log", "only": true }`: slice riêng; bỏ `only` để chạy tiếp.

## Kiểm thử

[tests/test_logsleuth.py](../tests/test_logsleuth.py) kiểm tra chuẩn thời gian, cấu hình, service, Unicode, cap toàn file và bridge Node. [tests/test_logsleuth.js](../tests/test_logsleuth.js) kiểm tra engine gốc (merge mtime, midnight, stack trace, không tràn file) và guard metadata. [tests/test_pipeline.py](../tests/test_pipeline.py) kiểm tra Auto/Manual end-to-end với Jira/AI giả lập; scan/slice thật.