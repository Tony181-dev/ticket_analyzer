# Ticket Analyzer

Tác giả: **hoang6.nguyen**

Phân tích Jira ticket bằng AI rồi đăng report lên Collab, chạy trên localhost với giao diện web (Tiếng Việt / English).
Gộp 3 extension cũ (Issue-Analyzer / local proxy M365, LogSleuth, CodeGraph) thành một app.

```
[1 Jira] → [2 AI hiểu ticket] → [3 Log brief] → [4 Code graph] → [5 AI phân tích] → [6 AI format] → [7 AI infographic] → [8 Đăng Collab]
  MCP        tìm timestamp ticket  LogSleuth        tuỳ chọn         M365 | CLI         Confluence XHTML   tuỳ chọn              create / update
```

Bước 2 đọc và diễn giải sơ bộ ticket ngay cả khi chưa có log (chế độ Tự nhập bỏ qua bước này). Bước 3 xử lý log chọn tay hoặc lấy từ attachment. Có Source code folder thì LogSleuth đối chiếu log với catalog câu lệnh trong source; nếu không có, nó vẫn tạo brief bằng từ khóa ticket và tín hiệu lỗi phổ biến. Bước 4 chạy riêng khi có source folder và bật **Dùng Code Graph**.

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
  - **Tự động (Skip)** — tải tối đa ảnh và log/archive trong **giới hạn tổng dung lượng**; ảnh ưu tiên trước, log AI xếp hạng trước rồi bổ sung các bộ log nhỏ còn vừa. Archive nhiều part là một bộ: tải đủ bộ hoặc bỏ cả bộ nếu quá lớn/thiếu part. AI trả danh sách rỗng không làm mất các ZIP/log có thể hữu ích. Log được đưa qua Log brief có giới hạn, không gửi nguyên archive lớn vào AI. Không có giờ sự cố thì dùng toàn khoảng log; không có log vẫn phân tích ticket/ảnh và chạy tiếp.
  - **Duyệt (Review)** — luồng dừng ở bước Log: AI điền sẵn tệp đề xuất (kèm lý do), khung giờ và múi giờ; bạn sửa/xác nhận rồi bấm **Tải & chạy tiếp**, hoặc **Bỏ qua log**. Tải lỗi thì quay lại hỏi bạn thay vì chết run. Đề xuất dừng ở **giới hạn tải tự động** như chế độ Tự động, nhưng phần bị bỏ vì vượt giới hạn hiện thành **cảnh báo** (tệp nào, bao nhiêu MB, gắn nhãn *vượt giới hạn*) với nút **Chọn thêm** — bạn quyết định có tải thêm không; chọn vượt giới hạn thì app hỏi xác nhận trước khi tải.
  - **Tự nhập (Manual)** — không hỏi AI ở bước 2: bạn tự chọn tệp cần tải, nhập giờ sự cố và chọn **múi giờ UTC của giờ đó** (ví dụ UTC+07:00), không cần nhập độ lệch phút. App căn sang giờ trong log sau khi tải. Để trống khung giờ để dùng toàn bộ log.
- **Run M365 hidden (headless)** ngay dưới nút chạy chỉ hiện ở M365, đồng bộ với Cấu hình. Worker dùng lại một tab chat; cùng session thì gửi tiếp trong tab đó, chuyển session mới chỉ điều hướng tab đang có.
- **Reset workflow** cạnh **Mở thư mục** có (?) và popup xác nhận: xóa kết quả các bước, nhật ký, token và thời gian; giữ đầu vào và attachment tải/giải nén. Hệ thống đang chạy thì nút bị khóa.
- Nút **Tiếp tục** chạy tiếp từ bước đang dang dở (không làm lại bước đã xong); mỗi bước cũng có **Chạy riêng bước này** / **Chạy từ bước này** trong drawer. Các dấu **(?)** giải thích từng lựa chọn. Khi bắt đầu một lượt chạy mới, bước bị lỗi ở lượt trước (mà lượt này sẽ chạy lại) trở về trạng thái chưa chạy, không còn đỏ.
- **Folder lưu trữ thông tin ticket** (dưới *Dùng Code Graph*): mọi file của ticket (Jira, khung giờ, log brief, phân tích, report, nhật ký chạy…) lưu tại folder này; tệp đính kèm tải vào folder con `attachments\`. Để trống (chữ mờ) thì như cũ: `data\runs\<run>` và thư mục tải đính kèm. Đổi folder thì file đã có được chép sang; xoá lịch sử không đụng tới folder này.
- **Múi giờ:** bước 2 hiểu giờ văn nói và chuẩn hóa, ví dụ “bug xảy ra ở 19:38 - 19:40 UTC+7” → `19:38:00`–`19:40:00`, múi giờ `+07:00`; không tự đổi giờ sang UTC hay đoán múi giờ còn thiếu. Trước khi cắt log, app so khung giờ với khoảng thực của từng file (artifact `03-log-span.md`) để căn đồng hồ log. Khung giờ rỗng thì quét toàn bộ log. Tên log được nhắc trong ticket có thể là file **bên trong ZIP**: không loại archive chỉ vì tên ngoài không khớp.
  - **Thêm khoảng giờ:** ở drawer bước 2, ngoài khoảng chính nhập tay thêm tối đa **2 khoảng** (cùng múi giờ, cùng độ lệch đồng hồ log); Log brief giữ dòng nằm trong bất kỳ khoảng nào. Các khoảng tự nhập được giữ khi AI đọc lại ticket.
  - **Ticket không nêu múi giờ:** ở chế độ Duyệt / Tự nhập, bước Log dừng sau khi log đã sẵn sàng và hỏi bạn: **theo gợi ý** (mỗi phương án cho biết khung giờ có nằm trong khoảng giờ của log không), **tự đặt** múi giờ của ticket và của log (xem trước khung giờ), hoặc **bỏ lọc theo giờ** (dùng toàn bộ log). Chế độ Tự động không dừng: căn tự động và ghi cảnh báo.
- **Trợ lý AI (bong bóng góc phải dưới):** chạy bằng Copilot CLI với model trong Cấu hình, nhìn thấy trạng thái hệ thống (ticket đang mở, các bước, lỗi, tệp đính kèm, log, cấu hình). Hỏi bất cứ gì hoặc bấm **Quét**; trợ lý gợi ý tải/bỏ tệp nào, khung giờ & múi giờ, nguồn code liên quan, cấu hình cần chỉnh, và tạo **bug ticket Markdown** cho lỗi của chính tool (`data/bugs/`). Mọi đề xuất hiện thành nút **Thực hiện** — không gì thay đổi khi bạn chưa bấm. Khi luồng dừng chờ chọn bằng chứng / múi giờ hoặc một bước lỗi, trợ lý tự quét một lần (tắt được trong Cấu hình).
- Bước đang chạy có bánh răng quay. Bấm vào một bước để xem file kết quả: brief log, catalog, sơ đồ code graph tương tác,
  phân tích, bản xem trước report. Có các nút Copy / Tải về / Mở tab mới / Mở thư mục.
- **Tệp đính kèm** (trong bước Jira): chọn file cần **Tải** và file cần **Phân tích**, chọn thư mục tải
  (để trống thì dùng `Downloads\TicketAnalyzer\<ISSUE>`; có *Folder lưu trữ thông tin ticket* thì cố định là `<folder>\attachments`). File đã tải đủ dung lượng thì không tải lại.
  Tải bằng **axel** đi kèm tool (`app\axel-for-windows-master`, tối đa 8 kết nối/tệp, ít hơn với tệp nhỏ): app hỏi server dung lượng thật và đường dẫn cuối (sau redirect) trước, rồi mới gọi axel — kể cả khi có giới hạn dung lượng. Tệp < 1 MB, server không hỗ trợ Range, HTTPS (axel 2.4 không hỗ trợ) hoặc axel lỗi → tải HTTP thường; nhật ký ghi `[axel ×N]` / lý do dùng HTTP.
  Nút **Tiến độ tải / giải nén tệp đính kèm** mở hai tab: **Tệp từ ticket** (mặc định, chọn tệp và bấm **Tải về → Giải nén** ngay sau bước Jira) và **File sau giải nén** (quét các file đã tải/giải nén, bấm **Giải nén tiếp** cho archive bên trong). Kết quả từng tệp vẫn hiện sau khi xong; file đã tải thành công được giữ khi dừng. Giải nén lỗi không được báo là thành công; thư mục còn dở hoặc archive đã đổi được giải nén lại khi thử tiếp.
  File nén (zip/tar/7z/rar) được giải nén; các file log bên trong được đưa vào bước Log brief.
- **Ảnh đính kèm:** được tải và đưa vào AI phân tích riêng, không đưa vào LogSleuth. M365 đọc tối đa **3 ảnh mỗi prompt**, đưa hướng dẫn và context ticket trực tiếp vào prompt để dành cả 3 slot cho ảnh; phân tích riêng từng ảnh rồi đối chiếu cả nhóm và tổng hợp thành `05-image-evidence.md`. CLI giữ luồng đọc ảnh hiện tại. Ticket chỉ có ảnh vẫn chạy đầy đủ phần phân tích/report, bỏ qua xử lý log. Giới hạn tải tự động có thể chỉnh trong Cấu hình; `0` nghĩa là không giới hạn. Tệp chưa rõ dung lượng không được tự chọn khi có giới hạn; Duyệt/Tự nhập cho phép chọn tay và hiển thị cảnh báo vượt giới hạn tự động.
- **AI hiểu ticket** chỉ đọc Jira và danh sách attachment, trình bày context, vấn đề được mô tả, nhận định ban đầu, điều chưa rõ và kế hoạch kiểm tra bằng chứng để đọc trong lúc chờ report. Chưa đọc log/ảnh/source nên chưa kết luận nguyên nhân. Khung giờ và lựa chọn attachment vẫn lưu nội bộ trong `02-focus.json`, không hiển thị JSON trong tab Hiểu ticket. Thiếu/sai timestamp: Auto match toàn catalog trên toàn khoảng log.
- **LogSleuth Auto / Manual:** Auto tự scan source và slice theo timestamp bước trước với cấu hình mặc định. Manual dừng ở bước Log brief để thao tác **Scan → chọn catalog/service → chọn khung giờ/tuning → Slice**; xem brief rồi bấm **Proceed** để sang bước tiếp theo. Xem [hướng dẫn tích hợp](docs/logsleuth-integration.md).
- **Token cap:** toàn bộ log brief (kể cả bảng/heading) có trần 50.000, có thể giảm ở Settings hoặc từng run. Dùng cận trên bảo thủ theo UTF-8 byte, không chỉ ước lượng `chars/4`. Ticket/graph/lịch sử AI vẫn dùng context riêng, nên giảm ngân sách nếu context tổng lớn.
- **AI phân tích** dùng high reasoning/long context với Copilot CLI. Khi LogSleuth match được câu lệnh log trong source, app gửi thêm đoạn code quanh chính các `file:line` đó; graph chỉ là chỉ mục cấu trúc, không được coi là bằng chứng hành vi của hàm. Phân tích có mục **Suggested Solution and Code Location**: source có liên quan không, vị trí nghi ngờ (`file:line`, symbol, độ tin cậy), hướng sửa và phạm vi ảnh hưởng.
- **Step 07 · AI infographic** bật/tắt được, dùng với cả M365 và Copilot CLI. M365 được yêu cầu tạo ảnh thật, tool chờ và tải ảnh về kể cả khi phản hồi không có văn bản. Nếu M365 lỗi hoặc không tải được ảnh, bước báo lỗi để chạy lại; không dựng SVG thay cho ảnh M365. Copilot CLI luôn dùng đường SVG (theo [instructions/infographic.md](instructions/infographic.md): mức độ, độ tin cậy, số liệu chính, dòng thời gian, bằng chứng, vị trí trong code, hành động — layout tự giãn theo nội dung). Ảnh chỉ là trang trí: publish **không bao giờ** bị chặn vì thiếu ảnh — trang vẫn đăng, ghi chú nêu rõ thiếu infographic, và chạy lại bước 7 rồi publish sẽ đính ảnh vào đúng trang đó.
- **Token & thời gian** (tab cạnh Nhật ký): mỗi lần bấm Chạy là một **lượt**; chạy lại ticket cộng thêm lượt mới chứ không ghi đè, tổng token = cộng mọi lượt. Copilot CLI đo thật; M365 không báo usage nên hiển thị **ước lượng** (byte/4, đánh dấu ~).
- **Automation:** `python -m app run ABC-1234 --publish --json` (hoặc `TicketAnalyzer.exe run …`) chạy không cần giao diện; REST API điều khiển app đang chạy. Chi tiết: [docs/automation.md](docs/automation.md).
- Source code folder được dùng chung để tạo catalog cho LogSleuth và (nếu bật) chạy Code Graph. Code Graph có checkbox riêng nên có thể tắt mà vẫn giữ catalog cho Log brief; nếu không chọn source folder, LogSleuth dùng chế độ tìm log dự phòng.
- Sửa được kết quả **AI phân tích** và **XHTML report**. Sau khi lưu, các bước sau tự đánh dấu *Cần chạy lại*.
- Đổi tùy chọn chỉ đánh dấu lại các bước bị ảnh hưởng. Nếu bật/tắt rồi quay về cấu hình cũ trước khi chạy hoặc sửa kết quả, trạng thái, ghi chú và thời gian của các bước được khôi phục, kể cả sau khi tải lại trang. Các thay đổi cấu hình khác chưa hoàn tác vẫn được tính; bật/tắt nhanh được lưu theo đúng thứ tự.
- Mặc định app dừng trước bước 8 để duyệt rồi mới **Đăng lên Collab**. Nếu trang trùng tên đã tồn tại thì app cập nhật trang đó.
- Infographic được gửi lên Collab bằng nội dung Base64, không phụ thuộc đường dẫn máy chạy MCP. App kiểm tra cả lỗi lồng trong phản hồi và đọc lại attachment đúng tên/dung lượng trước khi báo **đã đính kèm**; upload lỗi thì report vẫn đăng nhưng không chèn ảnh hỏng.

## Hai chế độ AI

| | M365 Copilot | Copilot CLI |
|---|---|---|
| Cách gọi | Playwright điều khiển m365.cloud.microsoft/chat, đính kèm file (tối đa 3 file mỗi prompt: khi nhiều hơn, app gộp các file bằng chứng thành 2 file, instruction giữ riêng) | `copilot -p … -s`, chỉ cấp quyền đọc file trong thư mục run |
| Model | Chọn theo tên hiển thị trong M365 (mặc định GPT-5.6 Sol Think Deeper) | `--model`, để trống = auto |
| Infographic | Ảnh do M365 Copilot tạo | Nội dung tóm tắt do CLI tạo, render thành SVG cục bộ |
| Đăng nhập | Nút “Đăng nhập M365” | `copilot login` hoặc `gh auth login` |

M365 gửi các file văn bản nội bộ (Markdown/JSON/log…) qua bản sao `.txt` giữ nguyên byte, kèm ánh xạ tên file trong prompt; file gốc và giới hạn tải vẫn được giữ. Tool chọn ô upload phù hợp loại file và nhận diện cả thông báo chặn trong tooltip **“Remove attachments to send”**. Nếu model/phiên/tài khoản vẫn chặn attachment, bước dừng với lý do cụ thể để đổi cấu hình thay vì tự đổi model hoặc thử lại cùng tệp. Trang đóng trước khi gửi được phục hồi một lần; nếu đã bấm gửi thì không tự gửi lại để tránh trùng prompt.

Trước mỗi lượt M365, tool dọn attachment chưa gửi trong composer của phiên đang dùng, nhập prompt và tải cả bộ file trong một lần chọn nếu ô upload hỗ trợ nhiều tệp. Tool xác nhận từng tên file trong khay đính kèm của composer mới (`chat-input-wrapper`), rồi kiểm tra lại đủ bộ ngay trước khi gửi. Bản sao của `01-jira.md` có tên `01-jira.md.txt` (M365 có thể ẩn đuôi `.txt`), không thêm tiền tố số lần nữa.

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

- **Bỏ qua bước:** bấm vào thẻ bước để mở bảng thông tin bên phải, rồi chọn **Bỏ qua** viền đỏ và xác nhận **Yes / No**. Bước đang chạy sẽ dừng và thu dọn xong trước khi chuyển tiếp; các bước khác tạm khóa khi workflow đang chạy. Bỏ qua bước cuối kết thúc luồng. File và nội dung đã đăng được giữ lại; tùy chọn tự động đăng Collab vẫn được áp dụng. Chạy lại toàn bộ sẽ thử lại các bước đã bỏ qua.
- **Model:** nhận tên không phân biệt hoa/thường và dấu cách/gạch nối, ví dụ `GPT-5.6 Sol Think Deeper` và `GPT 5.6 Sol Think deeper`. Model đã biết được lưu bằng tên chuẩn; Playwright cũng đối chiếu nhãn tương đương khi chọn và xác nhận trên web, giữ riêng **Think** với **Think Deeper**.
  Trong M365, model chỉ được chọn ở lần gọi AI đầu tiên của phiên. Các prompt/bước tiếp theo, kể cả tạo infographic, dùng lại model đã chọn; phiên đã lưu cũng giữ model khi worker khởi động lại. Chỉ chọn lại khi lựa chọn trước thất bại, phiên mới/reset, hoặc tab/phiên cũ không khôi phục được.
- **Trợ lý AI tự động:** checkbox bên dưới **Tự động đẩy lên Collab** đồng bộ với **Trợ lý AI tự quét…** trong Cấu hình. Tắt để ngăn các lượt tự quét mới, gồm lượt đang chờ; vẫn có thể chủ động hỏi trợ lý.
- **Reasoning:** tab bên cạnh **Token & thời gian** hiển thị reasoning/tiến trình công khai mà Copilot CLI phát ra trực tiếp, kèm bước và lượt chạy; lưu tối đa 600 sự kiện gần nhất trong journal giới hạn 4 MiB. Delta được cập nhật vào cùng một khối, không lặp lại bản hoàn chỉnh. Model/CLI không phát trace sẽ có thông báo rõ ràng. M365 chỉ ghi lời gọi AI local (như trợ lý CLI), không truy vết reasoning của chat web. **Reset workflow** xóa journal này cùng token/thời gian.
  Bộ đọc dùng [schema sự kiện công khai của GitHub](https://docs.github.com/en/copilot/how-tos/copilot-sdk/features/streaming-events); không lưu trường reasoning mã hóa, đối số hay kết quả thô của tool.
- **Kiểm tra tải file:** HTTP chỉ công nhận tải xong sau khi đối chiếu số byte với `Content-Length` (nếu có), từ chối phản hồi một phần và trang HTML đăng nhập thay cho file. File lỗi `.part` bị xóa, bản file cũ được giữ; ngân sách tổng áp dụng cả file đã tải sẵn. Ảnh M365 được kiểm tra HTTP/độ dài ở nhánh tải dự phòng và giải mã ảnh trước khi lưu, tránh ảnh tải thiếu chỉ có magic header.
- **Lịch sử:** rê chuột hoặc dùng phím Tab vào từng ticket để hiện nút thùng rác. Xác nhận **Yes / Cancel** chỉ xóa mục đã chọn; tác vụ đang chạy, thư mục dữ liệu riêng và file tải về Downloads được giữ lại.
  Phiên đang chạy có bánh răng quay, nhãn **Đang chạy** và viền vàng, kể cả khi đang xem ticket khác; hoàn tất/dừng thì tự bỏ dấu hiệu này.
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
