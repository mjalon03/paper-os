(() => {
  "use strict";
  const form = document.getElementById("search-form");
  const query = document.getElementById("query");
  const includePersonal = document.getElementById("include-personal");
  const status = document.getElementById("status");
  const results = document.getElementById("results");
  if (!form || !query || !includePersonal || !status || !results) return;

  const labels = {
    source_text: "논문 본문",
    reviewed_knowledge: "검토한 지식",
    user_annotation: "내 메모",
  };
  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };

  const render = (hits) => {
    clear(results);
    status.textContent = hits.length ? `${hits.length}개의 근거를 찾았습니다.` : "일치하는 근거가 없습니다.";
    for (const hit of hits) {
      const chunk = hit.chunk || {};
      const card = document.createElement("article");
      card.className = "result";
      const title = document.createElement("h2");
      title.textContent = hit.paper_title || "Untitled Paper";
      const pill = document.createElement("span");
      pill.className = "pill";
      pill.textContent = labels[chunk.source_kind] || "근거";
      const excerpt = document.createElement("p");
      excerpt.textContent = hit.text || "";
      const actions = document.createElement("div");
      actions.className = "actions";
      const reader = document.createElement("a");
      reader.href = `/read/${encodeURIComponent(hit.paper_id)}/v2#node-${encodeURIComponent(chunk.node_id || "")}`;
      reader.textContent = "본문 위치 열기";
      const ask = document.createElement("a");
      ask.href = `/knowledge/${encodeURIComponent(hit.paper_id)}#questions`;
      ask.textContent = "이 논문에 질문";
      actions.append(reader, ask);
      card.append(title, pill, excerpt, actions);
      results.appendChild(card);
    }
  };

  const run = async () => {
    const value = query.value.trim();
    if (!value) return;
    status.textContent = "서버에서 찾는 중…";
    clear(results);
    const parameters = new URLSearchParams({
      q: value,
      limit: "20",
      include_personal_annotations: String(includePersonal.checked),
    });
    try {
      const response = await fetch(`/api/search?${parameters}`, {credentials:"same-origin"});
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "search_failed");
      render(payload.hits || []);
      const url = new URL(window.location.href);
      url.searchParams.set("q", value);
      history.replaceState(null, "", url);
    } catch (_) {
      status.textContent = "검색을 완료하지 못했습니다. 검색 색인이 준비됐는지 확인해 주세요.";
    }
  };

  form.addEventListener("submit", (event) => { event.preventDefault(); run(); });
  const initial = new URLSearchParams(window.location.search).get("q");
  if (initial) { query.value = initial; run(); }
})();
