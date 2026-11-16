/* "How it works" infographic: flow (color-coded by subsystem), architecture, data. */
(function () {
  const C = { mcp: 'var(--c-mcp)', ai: 'var(--c-ai)', log: 'var(--c-log)', code: 'var(--c-code)', local: 'var(--c-local)' };

  const TXT = {
    vi: {
      title: 'Hệ thống vận hành thế nào',
      intro: 'Ticket Analyzer chạy hoàn toàn trên máy bạn (localhost). Nó lấy ticket từ Jira, gom bằng chứng từ log và source, nhờ AI phân tích, rồi đăng report lên Collab. Chế độ xử lý quyết định ai ra quyết định: Tự động (AI tự quyết, bỏ qua bước không làm được), Duyệt (AI đề xuất, bạn xác nhận) hoặc Tự nhập (bạn tự chọn).',
      flow: 'Luồng 8 bước',
      arch: 'Kiến trúc',
      data: 'Dữ liệu nằm ở đâu',
      legend: { mcp: 'Jira / Collab (MCP)', ai: 'AI (M365 hoặc Copilot CLI)', log: 'LogSleuth', code: 'CodeGraph', local: 'Xử lý cục bộ' },
      steps: [
        ['Jira ticket', 'Lấy ticket + danh sách đính kèm', 'mcp'],
        ['AI hiểu ticket', 'Tìm giờ sự cố + múi giờ trong ticket, chọn tệp đính kèm đáng tải', 'ai'],
        ['Log brief', 'Căn giờ ticket sang đồng hồ của log rồi cắt; không có giờ thì quét toàn bộ', 'log'],
        ['Code graph', 'Bản đồ hàm/lớp (Python, C/C++, Java, Kotlin, JS/TS), ưu tiên symbol liên quan ticket', 'code'],
        ['AI phân tích', 'Tổng hợp mọi bằng chứng', 'ai'],
        ['AI format', 'Report Confluence + tự sửa định dạng', 'ai'],
        ['AI infographic', 'M365 tạo ảnh (lỗi thì dựng SVG cục bộ); Copilot CLI luôn dùng SVG. Thiếu ảnh không chặn đăng', 'ai'],
        ['Đăng Collab', 'Tạo trang hoặc cập nhật trang cũ', 'mcp'],
      ],
      optional: 'tuỳ chọn',
      you: ['Bạn', 'Trình duyệt', ['Nhập ticket, chọn log / source', 'Xem tiến độ từng bước', 'Xem, sửa kết quả trước khi đăng']],
      server: ['Server cục bộ', '127.0.0.1:6969 · Python', [
        ['Pipeline', 'chạy 8 bước, dừng / chạy lại từng bước'],
        ['Setup Doctor', 'kiểm tra & chỉ cài thứ còn thiếu'],
        ['Lưu trữ', 'mỗi lần chạy một thư mục kết quả'],
      ]],
      links: { ui: 'HTTP + SSE (tiến độ realtime)', mcp: 'stdio JSON-RPC · uvx mcp-atlassian', m365: 'Playwright → Chromium', cli: 'copilot -p (chỉ đọc file)', tools: 'chạy cục bộ' },
      ext: [
        ['mcp', 'Jira & Collab', 'jira.lge.com · collab.lge.com', ['Lấy ticket, tải đính kèm', 'Tạo / cập nhật trang report']],
        ['ai', 'AI', 'chọn 1 trong 2 chế độ', ['M365 Copilot web (mặc định GPT 6 Luna)', 'GitHub Copilot CLI']],
        ['log', 'LogSleuth', 'Node.js', ['Catalog tạo từ source code folder đã chọn', 'Lọc theo thời điểm trong ticket nếu có; nếu không thì không lọc giờ']],
        ['code', 'CodeGraph', 'Python', ['Bật/tắt bằng checkbox riêng', 'Sơ đồ tương tác + bản rút gọn cho AI']],
      ],
      facts: [
        ['Token Jira/Collab', 'chỉ lưu trên máy này (data/config/mcp.json), không gửi lên trình duyệt.'],
        ['Kết quả mỗi lần chạy', 'data/runs/<ISSUE>-<thời gian>/ : ticket, log brief, phân tích, report, nhật ký.'],
        ['File đính kèm', 'Downloads\\TicketAnalyzer\\<ISSUE> (hoặc thư mục bạn chọn); file nén được giải nén tại chỗ.'],
        ['Gửi ra ngoài', 'chỉ Jira/Collab (qua MCP) và dịch vụ AI bạn chọn. Report xem trước không gửi đi đâu.'],
      ],
      author: 'Tác giả',
    },
    en: {
      title: 'How the system works',
      intro: 'Ticket Analyzer runs entirely on your machine (localhost). It fetches the Jira ticket, gathers evidence from logs and source code, lets AI analyze it, and publishes the report to Collab. The handling mode decides who decides: Auto (the AI, skipping undoable steps), Review (the AI suggests, you confirm) or Manual (you choose).',
      flow: 'The 8-step flow',
      arch: 'Architecture',
      data: 'Where your data lives',
      legend: { mcp: 'Jira / Collab (MCP)', ai: 'AI (M365 or Copilot CLI)', log: 'LogSleuth', code: 'CodeGraph', local: 'Local processing' },
      steps: [
        ['Jira ticket', 'Ticket + attachment list', 'mcp'],
        ['AI reads ticket', 'Finds the incident time + zone in the ticket, picks the attachments worth downloading', 'ai'],
        ['Log brief', 'Aligns the ticket time to the log’s clock, then slices; no time means a full scan', 'log'],
        ['Code graph', 'Map of functions/classes (Python, C/C++, Java, Kotlin, JS/TS), ticket-related symbols first', 'code'],
        ['AI analysis', 'Combines all the evidence', 'ai'],
        ['AI format', 'Confluence report + automatic format repair', 'ai'],
        ['AI infographic', 'M365 image (local SVG fallback on failure); Copilot CLI always SVG. A missing image never blocks publishing', 'ai'],
        ['Publish to Collab', 'Creates the page or updates the existing one', 'mcp'],
      ],
      optional: 'optional',
      you: ['You', 'Browser', ['Enter a ticket, pick logs / source', 'Watch each step live', 'Review & edit before publishing']],
      server: ['Local server', '127.0.0.1:6969 · Python', [
        ['Pipeline', 'runs the 8 steps; stop / re-run any step'],
        ['Setup Doctor', 'checks & installs only what is missing'],
        ['Storage', 'one result folder per run'],
      ]],
      links: { ui: 'HTTP + SSE (live progress)', mcp: 'stdio JSON-RPC · uvx mcp-atlassian', m365: 'Playwright → Chromium', cli: 'copilot -p (read-only)', tools: 'runs locally' },
      ext: [
        ['mcp', 'Jira & Collab', 'jira.lge.com · collab.lge.com', ['Fetch ticket, download attachments', 'Create / update the report page']],
        ['ai', 'AI', 'pick one of two modes', ['M365 Copilot web (default GPT 6 Luna)', 'GitHub Copilot CLI']],
        ['log', 'LogSleuth', 'Node.js', ['Catalog from the selected source folder', 'Filters by ticket time if available; otherwise no time filter']],
        ['code', 'CodeGraph', 'Python', ['Enabled/disabled by a separate checkbox', 'Interactive map + compact view for AI']],
      ],
      facts: [
        ['Jira/Collab tokens', 'stay on this machine (data/config/mcp.json) and are never sent to the browser.'],
        ['Per-run results', 'data/runs/<ISSUE>-<time>/ : ticket, log brief, analysis, report, log.'],
        ['Attachments', 'Downloads\\TicketAnalyzer\\<ISSUE> (or a folder you choose); archives are extracted in place.'],
        ['Leaves your machine', 'only Jira/Collab (via MCP) and the AI service you picked. The report preview is offline.'],
      ],
      author: 'Author',
    },
    ko: {
      title: '시스템 작동 방식',
      intro: 'Ticket Analyzer는 전부 이 PC(localhost)에서 실행됩니다. Jira 티켓을 가져오고, 로그와 소스 코드에서 근거를 모아 AI가 분석한 뒤 보고서를 Collab에 게시합니다. 처리 모드가 누가 결정할지를 정합니다: 자동(AI가 결정, 불가능한 단계는 건너뜀), 검토(AI가 제안, 사용자가 확인), 수동(사용자가 선택). 보고서는 화면 언어(한국어/English/Tiếng Việt)로 작성됩니다.',
      flow: '8단계 흐름',
      arch: '아키텍처',
      data: '데이터 위치',
      legend: { mcp: 'Jira / Collab (MCP)', ai: 'AI (M365 또는 Copilot CLI)', log: 'LogSleuth', code: 'CodeGraph', local: '로컬 처리' },
      steps: [
        ['Jira 티켓', '티켓 + 첨부 파일 목록', 'mcp'],
        ['AI 티켓 이해', '티켓에서 사고 시각과 시간대를 찾고, 받을 만한 첨부 파일을 고름', 'ai'],
        ['로그 브리프', '티켓 시각을 로그 시각에 맞춘 뒤 자름(시간대가 없으면 검토/수동 모드에서 선택 요청), 시각이 없으면 전체 스캔', 'log'],
        ['코드 그래프', '함수/클래스 지도(Python, C/C++, Java, Kotlin, JS/TS), 티켓 관련 심볼 우선', 'code'],
        ['AI 분석', '모든 근거를 종합하고 관련 코드 위치와 수정 방향 제시', 'ai'],
        ['AI 서식화', 'Confluence 보고서 + 자동 서식 복구', 'ai'],
        ['AI 인포그래픽', 'M365 이미지(실패 시 로컬 SVG), Copilot CLI는 항상 SVG. 이미지가 없어도 게시는 막히지 않음', 'ai'],
        ['Collab 게시', '페이지를 만들거나 기존 페이지를 업데이트', 'mcp'],
      ],
      optional: '선택',
      you: ['사용자', '브라우저', ['티켓 입력, 로그 / 소스 선택', '각 단계를 실시간으로 확인', '게시 전 검토 및 편집', 'AI 어시스턴트(오른쪽 아래)에게 질문']],
      server: ['로컬 서버', '127.0.0.1:6969 · Python', [
        ['파이프라인', '8단계 실행, 어느 단계든 중지 / 재실행'],
        ['Setup Doctor', '없는 것만 점검하고 설치'],
        ['저장소', '실행마다 결과 폴더 하나'],
      ]],
      links: { ui: 'HTTP + SSE (실시간 진행)', mcp: 'stdio JSON-RPC · uvx mcp-atlassian', m365: 'Playwright → Chromium', cli: 'copilot -p (읽기 전용)', tools: '로컬 실행' },
      ext: [
        ['mcp', 'Jira & Collab', 'jira.lge.com · collab.lge.com', ['티켓 가져오기, 첨부 파일 다운로드', '보고서 페이지 생성 / 업데이트']],
        ['ai', 'AI', '두 모드 중 선택', ['M365 Copilot 웹 (기본 GPT 6 Luna)', 'GitHub Copilot CLI']],
        ['log', 'LogSleuth', 'Node.js', ['선택한 소스 폴더로 카탈로그 생성', '티켓 시각이 있으면 시간으로 필터링, 없으면 시간 필터 없음']],
        ['code', 'CodeGraph', 'Python', ['별도 체크박스로 켜고 끔', '대화형 지도 + AI용 요약 보기']],
      ],
      facts: [
        ['Jira/Collab 토큰', '이 PC(data/config/mcp.json)에만 저장되며 브라우저로 보내지지 않습니다.'],
        ['실행별 결과', 'data/runs/<ISSUE>-<시각>/ : 티켓, 로그 브리프, 분석, 보고서, 로그.'],
        ['첨부 파일', 'Downloads\\TicketAnalyzer\\<ISSUE> (또는 선택한 폴더). 압축 파일은 그 자리에서 해제됩니다.'],
        ['외부로 나가는 것', 'Jira/Collab(MCP)과 선택한 AI 서비스뿐입니다. 보고서 미리 보기는 오프라인입니다.'],
        ['로컬 버그 티켓', 'AI 어시스턴트가 만든 도구 결함 티켓은 data/bugs/ 에 Markdown으로 저장됩니다.'],
      ],
      author: '제작',
    },
  };

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const ICON = ['s-jira', 's-understand', 's-log', 's-graph', 's-analyze', 's-format', 's-infographic', 's-collab'];

  function render(lang) {
    const T = TXT[lang] || TXT.vi;
    const legend = Object.entries(T.legend).map(([k, v]) => `<span class="ab-key"><i style="background:${C[k]}"></i>${esc(v)}</span>`).join('');
    const steps = T.steps.map(([name, desc, sys], i) => `
      <li class="ab-step" style="--c:${C[sys]}">
        <span class="ab-num">${i + 1}</span>
        <svg class="ab-ic"><use href="#${ICON[i]}"/></svg>
        <b>${esc(name)}</b><small>${esc(desc)}</small>
        ${(i >= 1 && i <= 3) || i === 6 ? `<em>${esc(T.optional)}</em>` : ''}
      </li>`).join('');
    const link = (cls, text, color) => `<div class="ab-link ${cls}" ${color ? `style="--c:${color}"` : ''}><span>${esc(text)}</span></div>`;
    const via = { mcp: T.links.mcp, ai: T.links.m365 + ' · ' + T.links.cli, log: T.links.tools, code: T.links.tools };
    const ext = T.ext.map(([sys, name, sub, items]) => `
      <div class="ab-row">
        ${link('', via[sys], C[sys])}
        <div class="ab-card ab-ext" style="--c:${C[sys]}">
          <div class="ab-card-h"><span class="ab-dot"></span><b>${esc(name)}</b><small>${esc(sub)}</small></div>
          <ul>${items.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
        </div>
      </div>`).join('');
    return `
      <p class="ab-intro">${esc(T.intro)}</p>
      <h4>${esc(T.flow)}</h4>
      <ol class="ab-flow">${steps}</ol>
      <div class="ab-legend">${legend}</div>

      <h4>${esc(T.arch)}</h4>
      <div class="ab-arch">
        <div class="ab-card ab-you">
          <div class="ab-card-h"><span class="ab-dot"></span><b>${esc(T.you[0])}</b><small>${esc(T.you[1])}</small></div>
          <ul>${T.you[2].map((x) => `<li>${esc(x)}</li>`).join('')}</ul>
        </div>
        ${link('ab-l1', T.links.ui)}
        <div class="ab-card ab-server">
          <div class="ab-card-h"><span class="ab-dot"></span><b>${esc(T.server[0])}</b><small>${esc(T.server[1])}</small></div>
          ${T.server[2].map(([a, b]) => `<div class="ab-mod"><b>${esc(a)}</b><span>${esc(b)}</span></div>`).join('')}
        </div>
        <div class="ab-exts">${ext}</div>
      </div>

      <h4>${esc(T.data)}</h4>
      <div class="ab-facts">${T.facts.map(([a, b]) => `<div class="ab-fact"><b>${esc(a)}</b><span>${esc(b)}</span></div>`).join('')}</div>
      <div class="ab-foot">Ticket Analyzer · ${esc(T.author)} <b>hoang6.nguyen</b></div>`;
  }

  window.About = { render, title: (lang) => (TXT[lang] || TXT.vi).title };
})();
