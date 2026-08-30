(() => {
  "use strict";
  const root = document.getElementById("knowledge-app");
  if (!root) return;
  const paperId = Number(root.dataset.paperId);
  const byId = (id) => document.getElementById(id);
  const statusNode = byId("status");
  const generateButton = byId("generate");
  const refreshButton = byId("refresh");
  const approveButton = byId("approve");
  const rejectButton = byId("reject");
  let overview = null;
  let pollTimer = null;
  let qaPollTimer = null;
  const qaStorageKey = `paper-qa-job:${paperId}`;
  let qaJobId = window.localStorage.getItem(qaStorageKey);

  const request = async (path, options = {}) => {
    const headers = new Headers(options.headers || {});
    if (options.body) headers.set("Content-Type", "application/json");
    if (options.method && options.method !== "GET") headers.set("X-Paper-Action", "1");
    const response = await fetch(path, {...options, headers, credentials: "same-origin"});
    let body = null;
    try { body = await response.json(); } catch (_) { body = {}; }
    if (!response.ok) {
      const error = new Error("request_failed");
      error.status = response.status;
      error.body = body;
      throw error;
    }
    return body;
  };

  const clear = (node) => { while (node.firstChild) node.removeChild(node.firstChild); };
  const list = (node, values) => {
    clear(node);
    for (const value of values || []) {
      const item = document.createElement("li");
      item.textContent = value;
      node.appendChild(item);
    }
  };
  const jobLabel = (job) => {
    if (!job) return "아직 만든 초안이 없습니다.";
    const labels = {queued:"초안 생성 대기 중",running:"DeepSeek Pro가 초안을 생성 중",retry_wait:"일시 오류로 재시도 대기 중",succeeded:"초안 생성 완료",failed:"초안 생성 실패"};
    return labels[job.status] || "상태를 확인할 수 없습니다.";
  };

  const render = (value) => {
    overview = value;
    const job = value.latest_job;
    const revision = value.latest_revision;
    const outbox = value.latest_outbox;
    const review = value.latest_review;
    const rejected = Boolean(revision && review && review.decision === "reject" && review.subject_revision_id === revision.id);
    statusNode.textContent = jobLabel(job);
    if (rejected) statusNode.textContent = "이 AI 초안은 반려되었습니다. 필요하면 새 초안을 만들 수 있습니다.";
    if (revision && revision.status === "reviewed") {
      statusNode.textContent = outbox && outbox.status === "delivered"
        ? "검토본이 Obsidian에 안전하게 저장되었습니다."
        : "검토 완료 · Mac의 Obsidian 반출 대기 중";
    }
    const usage = value.usage || {};
    const attempts = Number(usage.attempts || 0);
    const tokens = Number(usage.total_tokens || 0);
    byId("usage").textContent = attempts ? `API 사용 ${attempts}회 · 총 ${tokens.toLocaleString()} tokens · 비용은 제공자 가격표 연결 전까지 미확정` : "아직 API 사용 기록이 없습니다.";
    generateButton.disabled = Boolean(job && ["queued", "running", "retry_wait"].includes(job.status));

    if (!revision) {
      byId("draft-card").classList.add("hidden");
    } else {
      const payload = revision.canonical_payload;
      byId("draft-card").classList.remove("hidden");
      byId("revision-status").textContent = revision.status === "reviewed" ? "검토 완료" : rejected ? "반려됨" : "AI 초안";
      byId("summary").textContent = payload.summary;
      const claims = byId("claims");
      clear(claims);
      for (const claim of payload.claims || []) {
        const item = document.createElement("li");
        const text = document.createElement("div");
        text.textContent = claim.text;
        item.appendChild(text);
        for (const evidence of claim.evidence || []) {
          const quote = document.createElement("blockquote");
          const link = document.createElement("a");
          link.href = `/read/${paperId}/v2#node-${encodeURIComponent(evidence.node_id)}`;
          link.textContent = `“${evidence.exact_quote}” · 본문에서 보기`;
          quote.appendChild(link);
          item.appendChild(quote);
        }
        claims.appendChild(item);
      }
      list(byId("methods"), payload.key_methods);
      list(byId("findings"), payload.key_findings);
      list(byId("limitations"), payload.limitations);
      byId("review-actions").classList.toggle("hidden", revision.status !== "ai-draft" || rejected);
    }
    if (pollTimer) window.clearTimeout(pollTimer);
    if (job && ["queued", "running", "retry_wait"].includes(job.status)) {
      pollTimer = window.setTimeout(load, 3000);
    }
  };

  const showError = (error) => {
    const code = error && error.body && error.body.detail;
    if (error && error.message === "paper_text_and_cost_acknowledgement_required") {
      statusNode.textContent = "논문 본문 전송과 비용 안내를 확인한 뒤 체크해 주세요.";
      return;
    }
    statusNode.textContent = error.status === 409 ? "다른 화면에서 상태가 바뀌었습니다. 새로고침해 주세요." : `요청을 완료하지 못했습니다${typeof code === "string" ? `: ${code}` : "."}`;
  };

  const qaLabel = (job) => {
    const labels = {queued:"질문 대기 중",running:"DeepSeek Flash가 근거를 확인하는 중",retry_wait:"일시 오류로 재시도 대기 중",succeeded:"근거 검증이 끝난 답변입니다.",failed:"질문 처리가 실패했습니다."};
    return labels[job && job.status] || "아직 질문하지 않았습니다.";
  };
  const renderQa = (job) => {
    const status = byId("question-status");
    status.textContent = qaLabel(job);
    byId("ask").disabled = Boolean(job && ["queued", "running", "retry_wait"].includes(job.status));
    const answerCard = byId("answer-card");
    const answer = job && job.answer_payload;
    if (!answer || job.status !== "succeeded") {
      answerCard.classList.add("hidden");
    } else {
      answerCard.classList.remove("hidden");
      byId("answer").textContent = answer.status === "insufficient_evidence"
        ? `근거가 충분하지 않습니다: ${answer.insufficient_evidence_reason}`
        : answer.answer;
      const claims = byId("answer-claims");
      clear(claims);
      for (const claim of answer.claims || []) {
        const item = document.createElement("li");
        const text = document.createElement("div");
        text.textContent = claim.text;
        item.appendChild(text);
        for (const citation of claim.citations || []) {
          const quote = document.createElement("blockquote");
          const link = document.createElement("a");
          link.href = `/read/${paperId}/v2#node-${encodeURIComponent(citation.node_id)}`;
          const sources = {source_text:"논문 본문",reviewed_knowledge:"검토한 지식",user_annotation:"내 메모"};
          link.textContent = `“${citation.exact_quote}” · ${sources[citation.source_kind] || "근거"}에서 보기`;
          quote.appendChild(link);
          item.appendChild(quote);
        }
        claims.appendChild(item);
      }
    }
    if (qaPollTimer) window.clearTimeout(qaPollTimer);
    if (job && ["queued", "running", "retry_wait"].includes(job.status)) {
      qaPollTimer = window.setTimeout(loadQa, 3000);
    }
  };
  const showQaError = (error) => {
    const detail = error && error.body && error.body.detail;
    byId("question-status").textContent = detail === "search_index_not_ready"
      ? "검색 색인이 아직 준비되지 않았습니다. 잠시 후 다시 시도해 주세요."
      : "질문 상태를 확인하지 못했습니다.";
  };
  const loadQa = async () => {
    if (!qaJobId) { renderQa(null); return; }
    try {
      const payload = await request(`/api/papers/${paperId}/questions/${encodeURIComponent(qaJobId)}`);
      renderQa(payload.job);
    } catch (error) {
      if (error.status === 404) {
        window.localStorage.removeItem(qaStorageKey);
        qaJobId = null;
      }
      showQaError(error);
    }
  };
  const load = async () => {
    try { render(await request(`/api/papers/${paperId}/knowledge`)); }
    catch (error) { showError(error); }
  };
  const busy = async (button, action) => {
    button.disabled = true;
    try { await action(); await load(); }
    catch (error) { showError(error); }
    finally { button.disabled = false; }
  };

  generateButton.addEventListener("click", () => busy(generateButton, async () => {
    if (!byId("knowledge-consent").checked) {
      throw new Error("paper_text_and_cost_acknowledgement_required");
    }
    await request(`/api/papers/${paperId}/knowledge/jobs`, {
      method: "POST",
      body: JSON.stringify({
        idempotency_key: crypto.randomUUID(),
        include_personal_annotations: byId("include-annotations").checked,
        acknowledge_paper_text_and_cost: true,
      }),
    });
    byId("knowledge-consent").checked = false;
  }));
  refreshButton.addEventListener("click", load);
  approveButton.addEventListener("click", () => busy(approveButton, async () => {
    const revision = overview && overview.latest_revision;
    if (!revision || revision.status !== "ai-draft") throw new Error("no_draft");
    await request(`/api/knowledge/revisions/${revision.id}/approve`, {
      method: "POST",
      body: JSON.stringify({expected_review_revision: overview.review_revision, comment: null}),
    });
  }));
  rejectButton.addEventListener("click", () => busy(rejectButton, async () => {
    const revision = overview && overview.latest_revision;
    if (!revision || revision.status !== "ai-draft") throw new Error("no_draft");
    await request(`/api/knowledge/revisions/${revision.id}/reject`, {
      method: "POST",
      body: JSON.stringify({expected_review_revision: overview.review_revision, comment: null}),
    });
  }));
  byId("ask").addEventListener("click", async () => {
    const button = byId("ask");
    const question = byId("question").value.trim();
    if (!question) {
      byId("question-status").textContent = "질문을 먼저 입력해 주세요.";
      return;
    }
    button.disabled = true;
    byId("question-status").textContent = "질문 근거를 고르는 중…";
    try {
      const payload = await request(`/api/papers/${paperId}/questions`, {
        method: "POST",
        body: JSON.stringify({
          idempotency_key: crypto.randomUUID(),
          question,
          include_personal_annotations: byId("question-annotations").checked,
        }),
      });
      qaJobId = payload.job.id;
      window.localStorage.setItem(qaStorageKey, qaJobId);
      renderQa(payload.job);
    } catch (error) {
      showQaError(error);
    } finally {
      button.disabled = false;
    }
  });
  byId("question-refresh").addEventListener("click", loadQa);
  load();
  loadQa();
})();
