"""Mobile-first local search page for the private paper library."""

from __future__ import annotations


def render_search_page() -> str:
    return """<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <meta name="color-scheme" content="light dark">
  <title>논문 검색</title>
  <style>
    :root { color-scheme:light dark; font-family:-apple-system,BlinkMacSystemFont,"Pretendard",sans-serif; }
    body { margin:0; background:#f5f3ed; color:#20211f; }
    main { max-width:760px; margin:auto; padding:calc(18px + env(safe-area-inset-top)) 18px calc(44px + env(safe-area-inset-bottom)); }
    a { color:#2d6151; }
    h1 { margin:1rem 0 .5rem; font:700 clamp(1.8rem,8vw,2.8rem)/1.08 Iowan Old Style,Georgia,serif; }
    .muted { color:#66685f; font-size:.9rem; line-height:1.5; }
    .search { display:grid; grid-template-columns:1fr auto; gap:8px; margin:18px 0 10px; }
    input[type=search] { min-width:0; border:1px solid #d8d4ca; border-radius:13px; padding:13px; background:#fff; color:inherit; font:inherit; }
    button { border:0; border-radius:13px; padding:12px 16px; background:#285f50; color:#fff; font:inherit; font-weight:750; }
    label { display:flex; gap:8px; align-items:flex-start; }
    input[type=checkbox] { width:1.1rem; height:1.1rem; margin-top:.12rem; }
    #status { min-height:1.5rem; margin:18px 0 8px; font-weight:650; }
    .result { background:#fff; border:1px solid #dedbd2; border-radius:16px; padding:16px; margin:12px 0; }
    .result h2 { margin:0 0 .55rem; font-size:1.05rem; line-height:1.35; }
    .result p { margin:.6rem 0; line-height:1.55; overflow-wrap:anywhere; }
    .pill { display:inline-block; border-radius:999px; padding:4px 9px; background:#e5eee9; color:#285f50; font-size:.78rem; font-weight:700; }
    .actions { display:flex; flex-wrap:wrap; gap:12px; margin-top:10px; font-weight:650; font-size:.9rem; }
    @media (prefers-color-scheme:dark) {
      body { background:#171916; color:#eee; } input[type=search],.result { background:#22251f; border-color:#3b3e37; }
      .muted { color:#b8baaf; } .pill { background:#263b33; color:#a9d5c3; }
    }
  </style>
  <script defer src="/static/search.js"></script>
</head>
<body>
<main id="search-app">
  <a href="/library">← 서재</a>
  <h1>내 논문에서 찾기</h1>
  <p class="muted">이 검색은 서버 안에서만 실행되며 DeepSeek로 보내지 않습니다. 검토하지 않은 AI 초안은 검색 대상에서 제외됩니다.</p>
  <form id="search-form" class="search">
    <input id="query" type="search" maxlength="1000" required autocomplete="off" placeholder="개념, 방법, 결과를 입력">
    <button type="submit">검색</button>
  </form>
  <label class="muted"><input id="include-personal" type="checkbox" checked> 내 하이라이트와 메모도 로컬 검색에 포함</label>
  <div id="status" aria-live="polite"></div>
  <section id="results"></section>
</main>
</body>
</html>"""


__all__ = ["render_search_page"]
