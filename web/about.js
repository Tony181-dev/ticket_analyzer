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
