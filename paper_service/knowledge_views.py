"""Small mobile-first review surface for AI-generated paper knowledge."""

from __future__ import annotations

import html
from typing import Any


def render_knowledge_page(paper: dict[str, Any]) -> str:
    paper_id = int(paper["id"])
    title = html.escape(str(paper.get("title") or "Untitled Paper"))
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>지식 검토 · {title}</title>
  <style>
    :root {{ color-scheme: light dark; font-family: -apple-system,BlinkMacSystemFont,"Pretendard",sans-serif; }}
    body {{ margin:0; background:#f5f3ed; color:#20211f; }}
    main {{ max-width:760px; margin:auto; padding:calc(18px + env(safe-area-inset-top)) 18px calc(44px + env(safe-area-inset-bottom)); }}
    a {{ color:#2d6151; }}
    h1 {{ font-family:Iowan Old Style,Georgia,serif; font-size:clamp(1.55rem,7vw,2.3rem); line-height:1.14; margin:.7rem 0 1rem; }}
    h2 {{ font-size:1.05rem; margin:0 0 .75rem; }}
    .top {{ display:flex; gap:12px; justify-content:space-between; align-items:center; }}
    .card {{ background:#fff; border:1px solid #dedbd2; border-radius:16px; padding:16px; margin:14px 0; box-shadow:0 5px 20px rgba(38,36,30,.05); }}
    .notice {{ background:#fff8df; border-color:#ead699; line-height:1.55; }}
    .muted {{ color:#66685f; font-size:.91rem; line-height:1.5; }}
    .status {{ min-height:1.5rem; font-weight:650; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:9px; margin-top:12px; }}
    button {{ border:0; border-radius:12px; padding:11px 15px; font:inherit; font-weight:700; background:#285f50; color:white; }}
    button.secondary {{ background:#e9e7df; color:#30312e; }}
    button.danger {{ background:#8c3f3a; }}
    button:disabled {{ opacity:.48; }}
    label {{ display:flex; gap:9px; align-items:flex-start; line-height:1.45; }}
    input[type=checkbox] {{ width:1.15rem; height:1.15rem; margin-top:.15rem; }}
    textarea {{ box-sizing:border-box; width:100%; min-height:7rem; resize:vertical; border:1px solid #d8d4ca; border-radius:12px; padding:12px; background:#fff; color:inherit; font:inherit; line-height:1.5; }}
    ul,ol {{ padding-left:1.35rem; }}
    li {{ margin:.58rem 0; line-height:1.55; }}
    blockquote {{ margin:.5rem 0 1rem; border-left:3px solid #c8c4b7; padding-left:12px; color:#55584f; }}
    .pill {{ display:inline-block; border-radius:999px; padding:4px 9px; background:#e5eee9; color:#285f50; font-size:.82rem; font-weight:700; }}
    .hidden {{ display:none !important; }}
    @media (prefers-color-scheme:dark) {{
      body {{ background:#171916; color:#eee; }} .card {{ background:#22251f; border-color:#3b3e37; }}
      .notice {{ background:#332f1e; border-color:#655b32; }} .muted,blockquote {{ color:#b8baaf; }}
      button.secondary {{ background:#393c35; color:#eee; }} .pill {{ background:#263b33; color:#a9d5c3; }} textarea {{ background:#1b1d19; border-color:#3b3e37; }}
    }}
  </style>
  <script defer src="/static/knowledge.js"></script>
</head>
<body>
<main id="knowledge-app" data-paper-id="{paper_id}">
  <div class="top"><a href="/read/{paper_id}/v2">← 논문으로</a><a href="/library">서재</a></div>
  <h1>{title}</h1>
  <section class="card notice">
    <strong>전송 범위</strong>
    <p class="muted">초안을 만들면 논문의 현재 모바일 본문 중 최대 8쪽·30,000자가 DeepSeek Pro로 전송됩니다. 개인 하이라이트와 메모는 아래 항목을 직접 켠 경우에만 포함됩니다. AI 결과는 승인 전에는 Obsidian으로 가지 않습니다.</p>
    <label><input id="knowledge-consent" type="checkbox"> 이번 요청에서 위 논문 본문 전송과 DeepSeek Pro 사용 비용 발생 가능성을 확인했습니다.</label>
    <label><input id="include-annotations" type="checkbox"> 내 하이라이트·메모도 이번 요청에 포함</label>
    <div class="actions"><button id="generate" type="button">지식 초안 만들기</button><button id="refresh" class="secondary" type="button">상태 새로고침</button></div>
  </section>
  <section class="card" aria-live="polite">
    <h2>현재 상태</h2>
    <div id="status" class="status">불러오는 중…</div>
    <div id="usage" class="muted"></div>
  </section>
  <section id="draft-card" class="card hidden">
    <div class="top"><h2>검토할 내용</h2><span id="revision-status" class="pill"></span></div>
    <h2>요약</h2><p id="summary"></p>
    <h2>핵심 주장과 근거</h2><ol id="claims"></ol>
    <h2>방법</h2><ul id="methods"></ul>
    <h2>주요 결과</h2><ul id="findings"></ul>
    <h2>한계</h2><ul id="limitations"></ul>
    <div id="review-actions" class="actions">
      <button id="approve" type="button">검토 완료 · Obsidian 대기열로</button>
      <button id="reject" class="danger" type="button">반려</button>
    </div>
  </section>
  <section id="questions" class="card">
    <h2>이 논문에 근거 기반 질문</h2>
    <p class="muted">질문하면 관련 본문과 검토 완료된 지식 중 검색 근거 최대 32개(계약상 총 상한 240,000자)가 DeepSeek Flash로 전송됩니다. 검토 전 AI 지식은 제외됩니다. 내 메모는 아래 항목과 각 메모의 ‘AI 공유 허용’이 모두 켜진 경우에만 포함됩니다.</p>
    <textarea id="question" maxlength="4000" placeholder="예: 이 논문의 핵심 가설을 지지하는 결과와 한계는 무엇인가?"></textarea>
    <label><input id="question-annotations" type="checkbox"> AI 공유를 허용한 내 메모도 이번 질문에 포함</label>
    <div class="actions"><button id="ask" type="button">DeepSeek Flash에 질문</button><button id="question-refresh" class="secondary" type="button">답변 상태 확인</button></div>
    <div id="question-status" class="status" aria-live="polite">아직 질문하지 않았습니다.</div>
    <section id="answer-card" class="hidden">
      <h2>답변</h2><p id="answer"></p>
      <h2>주장별 근거</h2><ol id="answer-claims"></ol>
    </section>
  </section>
</main>
</body>
</html>"""


__all__ = ["render_knowledge_page"]
