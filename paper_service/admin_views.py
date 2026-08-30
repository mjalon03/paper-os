"""Static shell for the private, mobile-first paper operations console."""

from __future__ import annotations


def render_admin_page() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <title>논문 운영</title>
  <style>
    :root {
      --paper: #f6f3ec;
      --surface: #fffdf8;
      --ink: #17242d;
      --muted: #64727a;
      --line: #d7ddd9;
      --navy: #16324a;
      --teal: #16766f;
      --teal-soft: #dcefeb;
      --amber: #9a650d;
      --amber-soft: #fff0c9;
      --danger: #a43c38;
      --danger-soft: #f9e2df;
      --ok: #347052;
      --shadow: 0 8px 24px rgba(23, 50, 74, .08);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--paper);
      color: var(--ink);
      line-height: 1.5;
      padding-bottom: calc(24px + env(safe-area-inset-bottom));
    }
    button, input, select { font: inherit; }
    button, select, input[type="text"] { min-height: 44px; }
    button {
      border: 0;
      border-radius: 12px;
      padding: 10px 14px;
      background: var(--navy);
      color: white;
      font-weight: 750;
      cursor: pointer;
    }
    button:disabled { cursor: wait; opacity: .55; }
    button.secondary { background: var(--teal-soft); color: var(--navy); }
    button.danger { background: var(--danger); }
    button.ghost { background: transparent; color: var(--navy); border: 1px solid var(--line); }
    button:focus-visible, input:focus-visible, select:focus-visible, summary:focus-visible {
      outline: 3px solid #61b8ad;
      outline-offset: 2px;
    }
    .skip-link { position: absolute; left: -9999px; }
    .skip-link:focus { left: 12px; top: 12px; z-index: 20; background: white; padding: 8px; }
    .topbar {
      position: sticky;
      top: 0;
      z-index: 10;
      padding: calc(12px + env(safe-area-inset-top)) 16px 12px;
      color: white;
      background: rgba(22, 50, 74, .96);
      backdrop-filter: blur(12px);
      box-shadow: var(--shadow);
    }
    .topbar-inner, main { width: min(100%, 920px); margin: 0 auto; }
    .topbar-inner { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
    .brand { display: flex; align-items: center; gap: 10px; }
    .mark { width: 35px; height: 35px; display: grid; place-items: center; border-radius: 11px; background: #d9eee9; color: var(--navy); font-weight: 900; }
    h1 { margin: 0; font-size: 1.12rem; letter-spacing: -.02em; }
    .eyebrow { margin: 0; font-size: .72rem; color: #bed6e0; }
    main { padding: 16px; }
    .notice {
      margin: 0 0 14px;
      padding: 11px 13px;
      border: 1px solid #e4c77c;
      border-radius: 13px;
      background: var(--amber-soft);
      color: #5f430f;
      font-size: .88rem;
    }
    .summary-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
    .metric { background: var(--surface); border: 1px solid var(--line); border-radius: 15px; padding: 13px; box-shadow: var(--shadow); }
    .metric strong { display: block; font-size: 1.45rem; color: var(--navy); }
    .metric span { font-size: .78rem; color: var(--muted); }
    section { margin-top: 22px; scroll-margin-top: 90px; }
    .section-head { display: flex; align-items: end; justify-content: space-between; gap: 10px; margin-bottom: 9px; }
    h2 { margin: 0; font-size: 1.05rem; letter-spacing: -.02em; }
    .section-copy { margin: 2px 0 0; color: var(--muted); font-size: .82rem; }
    .stack { display: grid; gap: 10px; }
    .card { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; padding: 14px; box-shadow: var(--shadow); overflow-wrap: anywhere; }
    .card h3 { margin: 0; font-size: .96rem; }
    .card p { margin: 6px 0 0; font-size: .86rem; }
    .meta { color: var(--muted); }
    .badge { display: inline-flex; align-items: center; min-height: 25px; border-radius: 999px; padding: 3px 9px; background: var(--teal-soft); color: #155d58; font-size: .73rem; font-weight: 760; }
    .badge.warning { background: var(--amber-soft); color: var(--amber); }
    .badge.danger { background: var(--danger-soft); color: var(--danger); }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
    details.panel { background: var(--surface); border: 1px solid var(--line); border-radius: 16px; box-shadow: var(--shadow); }
    details.panel > summary { min-height: 50px; padding: 13px 14px; cursor: pointer; font-weight: 800; }
    .panel-body { border-top: 1px solid var(--line); padding: 14px; }
    .field { display: grid; gap: 5px; margin-bottom: 12px; }
    .field label, .field legend { font-size: .84rem; font-weight: 760; }
    .field input[type="text"], .field select { width: 100%; border: 1px solid #aeb9b7; border-radius: 11px; padding: 9px 11px; background: var(--surface); color: var(--ink); }
    fieldset { border: 0; padding: 0; margin: 0 0 12px; }
    .paper-list { max-height: 280px; overflow: auto; display: grid; gap: 7px; border: 1px solid var(--line); border-radius: 12px; padding: 8px; }
    .paper-choice { display: grid; grid-template-columns: 24px 1fr; gap: 8px; align-items: start; padding: 7px; border-radius: 9px; }
    .paper-choice:has(input:checked) { background: var(--teal-soft); }
    .paper-choice input { width: 20px; height: 20px; margin: 2px 0 0; }
    .paper-choice strong { display: block; font-size: .83rem; }
    .paper-choice small { color: var(--muted); }
    .cost-note { padding: 10px; border-left: 4px solid var(--amber); border-radius: 8px; background: var(--amber-soft); color: #60430c; font-size: .8rem; }
    .empty { margin: 0; padding: 16px; border: 1px dashed #aeb9b7; border-radius: 13px; color: var(--muted); text-align: center; font-size: .85rem; }
    .mini-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; margin-top: 10px; }
    .mini-grid div { border-radius: 10px; padding: 8px; background: #eef2f1; }
    .mini-grid strong { display: block; }
    .toast { position: fixed; left: 16px; right: 16px; bottom: calc(16px + env(safe-area-inset-bottom)); z-index: 30; margin: auto; max-width: 560px; padding: 12px 14px; border-radius: 13px; background: var(--navy); color: white; box-shadow: var(--shadow); transform: translateY(160%); transition: transform .18s ease; }
    .toast.visible { transform: translateY(0); }
    .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0,0,0,0); white-space: nowrap; }
    @media (min-width: 720px) {
      .summary-grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .two-column { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    }
    @media (prefers-reduced-motion: reduce) { * { scroll-behavior: auto !important; transition: none !important; } }
    @media (prefers-color-scheme: dark) {
      :root { --paper: #111a20; --surface: #17242c; --ink: #edf4f1; --muted: #a9b8bb; --line: #34464b; --navy: #23536d; --teal-soft: #173f3e; --amber-soft: #493917; --danger-soft: #472726; }
      .mini-grid div { background: #223239; }
    }
  </style>
  <script src="/static/admin.js" defer></script>
</head>
<body>
  <a class="skip-link" href="#main">본문으로 이동</a>
  <header class="topbar">
    <div class="topbar-inner">
      <div class="brand"><span class="mark" aria-hidden="true">P</span><div><p class="eyebrow">PERSONAL PAPER OS</p><h1>논문 운영</h1></div></div>
      <button id="refresh" class="secondary" type="button">새로고침</button>
    </div>
  </header>
  <main id="main">
    <p class="notice">승인 버튼은 실제 작업을 대기열에 추가합니다. 적용 전 대상과 비용 안내를 다시 확인하세요.</p>
    <section aria-labelledby="summary-title">
      <div class="section-head"><div><h2 id="summary-title">지금 상태</h2><p class="section-copy" id="updated-at">불러오는 중…</p></div></div>
      <div class="summary-grid" id="summary-cards" aria-busy="true"></div>
    </section>

    <section aria-labelledby="pending-title">
      <div class="section-head"><div><h2 id="pending-title">내 확인이 필요해요</h2><p class="section-copy">승인 전에는 재처리나 되돌리기가 실행되지 않습니다.</p></div></div>
      <div class="stack" id="pending-operations" aria-live="polite"></div>
    </section>

    <section aria-labelledby="new-action-title">
      <div class="section-head"><div><h2 id="new-action-title">새 작업 만들기</h2><p class="section-copy">논문을 직접 골라 요청을 만든 뒤 별도로 승인합니다.</p></div></div>
      <div class="stack">
        <details class="panel" id="reprocess-panel">
          <summary>선택 논문 다시 처리</summary>
          <form class="panel-body" id="reprocess-form">
            <div class="field"><label for="paper-filter">논문 찾기</label><input id="paper-filter" type="text" autocomplete="off" placeholder="제목 또는 번호"></div>
            <fieldset class="field"><legend>다시 처리할 논문</legend><div class="paper-list" id="paper-choices" aria-busy="true"></div></fieldset>
            <div class="field"><label for="reprocess-reason">이유</label><select id="reprocess-reason"><option value="reader_quality_fix">모바일 읽기 품질 개선</option><option value="extractor_upgrade">추출기 개선 적용</option><option value="manual_reprocess">직접 재처리 요청</option></select></div>
            <button type="submit">선택 항목으로 요청 만들기</button>
          </form>
        </details>
        <details class="panel" id="rollback-panel">
          <summary>이전 모바일 버전으로 되돌리기</summary>
          <form class="panel-body" id="rollback-form">
            <div class="field"><label for="rollback-paper">논문</label><select id="rollback-paper"><option value="">논문을 선택하세요</option></select></div>
            <div class="field"><label for="rollback-version">되돌릴 버전</label><select id="rollback-version" disabled><option value="">먼저 논문을 선택하세요</option></select></div>
            <p class="cost-note">승인하면 현재 모바일 버전 포인터가 선택한 과거 버전으로 바뀝니다. 원본 PDF는 삭제하지 않습니다.</p>
            <button class="danger" type="submit">되돌리기 요청 만들기</button>
          </form>
        </details>
      </div>
    </section>

    <section aria-labelledby="taxonomy-title">
      <div class="section-head"><div><h2 id="taxonomy-title">폴더 구조 제안</h2><p class="section-copy">AI 요청과 실제 파일 이동 승인을 분리합니다.</p></div></div>
      <div class="stack" id="taxonomy-audits"></div>
      <div class="stack" id="taxonomy-proposals" style="margin-top:10px"></div>
    </section>

    <section aria-labelledby="health-title" class="two-column">
      <div><div class="section-head"><div><h2 id="health-title">작업자 상태</h2></div></div><div class="stack" id="worker-health"></div></div>
      <div><div class="section-head"><div><h2>사용량</h2></div></div><div class="stack" id="cost-summary"></div></div>
    </section>

    <section aria-labelledby="recent-title">
      <div class="section-head"><div><h2 id="recent-title">최근 요청</h2></div></div>
      <div class="stack" id="recent-operations"></div>
    </section>

    <section aria-labelledby="failures-title">
      <div class="section-head"><div><h2 id="failures-title">확인이 필요한 실패</h2></div></div>
      <div class="stack" id="operation-failures"></div>
    </section>
  </main>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>
</body>
</html>"""


__all__ = ["render_admin_page"]
