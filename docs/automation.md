# Tự động hoá Ticket Analyzer

Hai cách chạy không cần thao tác tay: **dòng lệnh** (một tiến trình, chạy xong thoát) và **REST API**
(app đang chạy nền, điều khiển từ script/CI). Cả hai dùng đúng pipeline của giao diện web.

## 1. Dòng lệnh

```bat
python -m app run ABC-1234
TicketAnalyzer.exe run ABC-1234 ABC-1235 --publish
python -m app run ABC-1234 --log D:\logs\main.log --source D:\src\project --json > result.json
```

Mỗi ticket chạy ở chế độ **Tự động (Skip)**: AI chọn tệp log đính kèm (trong giới hạn
`auto_download_max_mb`), tự tìm khung giờ và căn múi giờ; bước không làm được (không log, không
source, AI lỗi) bị bỏ qua và phân tích chạy tiếp với dữ liệu còn lại. Không bao giờ dừng chờ người.

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--log PATH` | thêm file log/archive (lặp lại được) |
| `--source DIR` | folder source (catalog LogSleuth + Code Graph) |
| `--no-attachments` / `--no-graph` / `--no-infographic` | tắt phần tương ứng |
| `--publish` / `--no-publish` | đăng Collab luôn / dừng khi report sẵn sàng (mặc định theo Settings) |
| `--until STEP` | dừng sau bước đó (`jira understand log graph analyze format infographic collab`) |
| `--shift HOURS` | lệch giờ log − ticket, ví dụ `--shift -9` (ticket KST, log UTC); bỏ trống = tự phát hiện |
| `--ai m365\|copilot` | chọn backend AI |
| `--json` | in một JSON tổng kết ra stdout (tiến trình ra stderr) |
| `--quiet` | không in tiến trình |

Exit code `0` khi mọi ticket ra được report (bước bắt buộc không lỗi), `1` khi có ticket hỏng,
`130` khi bị Ctrl+C. JSON tổng kết gồm `steps`, `errors`, `files` (đường dẫn
`05-analysis.md`, `06-report.xhtml`, preview), `collab_url` và `tokens` (tổng token của run).

Ví dụ chạy hằng đêm (Task Scheduler / CI):

```bat
TicketAnalyzer.exe run ABC-1234 --publish --json 1> out.json 2> progress.log
if errorlevel 1 echo Có ticket lỗi, xem out.json
```

## 2. REST API (app đang chạy)

Server chỉ bind `127.0.0.1` (mặc định cổng 6969). Các endpoint chính:

```text
POST /api/runs                 tạo run {issue, gate?, log_files?, source_dir?, auto_attachments?,
                               use_codegraph?, use_infographic?, auto_publish_to_collab?,
                               log_shift_minutes?, until?, start? (mặc định true)}
GET  /api/runs/<id>            trạng thái run (steps, pending_gate, usage_passes, usage_total, collab)
POST /api/runs/<id>/start      {from?, only?, until?, resume?, publish?}
POST /api/runs/<id>/inputs     đổi input (các trường như khi tạo)
POST /api/runs/<id>/gate       trả lời cổng duyệt {names[], time_start?, time_end?, shift_minutes?,
                               skip_logs?, folder?}
POST /api/runs/<id>/timezone   trả lời câu hỏi múi giờ (pending_tz) {mode: suggested|manual|auto|bypass,
                               shift_minutes?, time_start?, time_end?, ticket_utc_offset?}
GET  /api/runs/<id>/files/<f>  tải artifact (05-analysis.md, 06-report.xhtml, ...)
POST /api/stop                 dừng run đang chạy
GET  /api/events               SSE: run / log / busy / assistant*
GET  /api/assistant            hội thoại trợ lý AI, bug ticket nội bộ
POST /api/assistant/ask        {text?, run_id?, kind: chat|scan, reason?, trigger?}
POST /api/assistant/action     thực hiện một đề xuất {message_id, index}
```

Kịch bản tự động hoàn toàn: tạo run với `"gate": "skip"` rồi poll `GET /api/runs/<id>` (hoặc nghe
SSE `busy`) đến khi `busy` tắt. Kịch bản có người duyệt: `"gate": "review"`; khi run dừng,
`pending_gate` chứa `suggested` (tệp AI chọn) và `reasons`; trả lời bằng `POST .../gate`.

```bash
curl -s -X POST http://127.0.0.1:6969/api/runs \
  -H "Content-Type: application/json" \
  -d '{"issue": "ABC-1234", "gate": "skip", "auto_publish_to_collab": true}'
```

Lưu ý: chỉ một run chạy tại một thời điểm (M365 dùng chung một phiên trình duyệt); `POST /api/runs`
trả lỗi `err.busy` khi đang bận. Token mỗi lượt chạy nằm trong `usage_passes`; tổng ở `usage_total`.

## 3. Ba chế độ xử lý (gate)

| | Tự động (skip) | Duyệt (review) | Tự nhập (manual) |
|---|---|---|---|
| Chọn tệp log đính kèm | AI chọn, tự tải (giới hạn MB) | AI đề xuất, bạn xác nhận | bạn tự chọn |
| Khung giờ sự cố | AI đọc ticket | AI điền sẵn, bạn sửa được | bạn nhập |
| Múi giờ / lệch giờ log | tự so với khoảng giờ log, AI phá hoà khi cần | như Tự động, sửa tay được | bạn nhập (phút) |
| Khi thiếu log / bước lỗi | bỏ qua bước đó, chạy tiếp | dừng hỏi bạn (chọn lại tệp, thêm log, hoặc bỏ qua log) | như Duyệt |
| Dùng cho | automation, chạy hàng loạt | làm việc thường ngày | ticket khó, không tin AI |

Chế độ mặc định cho ticket mới đặt ở ⚙ Cấu hình (`default_gate`).
