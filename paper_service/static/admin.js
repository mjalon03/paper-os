(() => {
  "use strict";

  const ACTION_HEADER = "X-Paper-Action";
  const state = { overview: null, papers: [] };
  const byId = (id) => document.getElementById(id);

  function node(tag, className, text) {
    const value = document.createElement(tag);
    if (className) value.className = className;
    if (text !== undefined) value.textContent = String(text);
    return value;
  }

  function replace(container, children) {
    container.replaceChildren(...children);
  }

  function empty(message) {
    return node("p", "empty", message);
  }

  function badge(text, tone = "") {
    return node("span", `badge${tone ? ` ${tone}` : ""}`, text);
  }

  function formatNumber(value) {
    return new Intl.NumberFormat("ko-KR").format(Number(value || 0));
  }

  function formatDate(value) {
    if (!value) return "시간 정보 없음";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "시간 정보 없음";
    return new Intl.DateTimeFormat("ko-KR", {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    }).format(parsed);
  }

  function shortId(value) {
    const text = String(value || "");
    return text.length > 12 ? `${text.slice(0, 8)}…` : text;
  }

  function safeCode(value) {
    const text = String(value || "request_failed");
    return /^[a-z][a-z0-9_]{0,119}$/.test(text) ? text : "request_failed";
  }

  function detailCode(payload) {
    if (!payload || typeof payload !== "object") return "request_failed";
    const detail = payload.detail;
    if (typeof detail === "string") return safeCode(detail);
    if (detail && typeof detail === "object") return safeCode(detail.code);
    return "request_failed";
  }

  async function api(path, options = {}) {
    const headers = { Accept: "application/json", ...(options.headers || {}) };
    if (options.body !== undefined) {
      headers["Content-Type"] = "application/json";
      headers[ACTION_HEADER] = "1";
    }
    const response = await fetch(path, {
      method: options.method || "GET",
      credentials: "same-origin",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    let payload = null;
    try {
      payload = await response.json();
    } catch (_error) {
      payload = null;
    }
    if (!response.ok) {
      const error = new Error(detailCode(payload));
      error.code = detailCode(payload);
      throw error;
    }
    return payload;
  }

  let toastTimer = null;
  function toast(message) {
    const target = byId("toast");
    target.textContent = message;
    target.classList.add("visible");
    if (toastTimer) window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => target.classList.remove("visible"), 3200);
  }

  async function busy(button, action) {
    const prior = button.disabled;
    button.disabled = true;
    try {
      return await action();
    } catch (error) {
      toast(`완료하지 못했습니다 · ${safeCode(error.code)}`);
      throw error;
    } finally {
      button.disabled = prior;
    }
  }

  const stateLabels = {
    pending_review: "승인 대기",
    approved_not_enqueued: "승인됨",
    active: "진행 중",
    succeeded: "완료",
    failed: "실패",
    rejected: "반려",
    queued: "대기 중",
    running: "진행 중",
    retry_wait: "재시도 대기",
  };

  function operationKindLabel(kind) {
    return kind === "rollback" ? "버전 되돌리기" : "논문 다시 처리";
  }

  function renderSummary(overview) {
    const target = byId("summary-cards");
    target.setAttribute("aria-busy", "false");
    if (!overview.operations) {
      replace(target, [empty("운영 상태를 불러오지 못했습니다.")]);
      return;
    }
    const value = overview.operations;
    const active = Number(value.reprocess_active || 0) + Number(value.rollback_active || 0)
      + Number(value.taxonomy_generation_active || 0) + Number(value.taxonomy_move_active || 0);
    const failed = Number(value.reprocess_failed || 0) + Number(value.rollback_failed || 0)
      + Number(value.taxonomy_generation_failed || 0) + Number(value.taxonomy_move_failed || 0);
    const metrics = [
      ["열린 장애", value.open_incidents],
      ["내 승인 대기", Number(value.reprocess_pending_review || 0) + Number(value.rollback_pending_review || 0)],
      ["진행 중 작업", active],
      ["실패 작업", failed],
    ];
    replace(target, metrics.map(([label, amount]) => {
      const card = node("div", "metric");
      card.append(node("strong", "", formatNumber(amount)), node("span", "", label));
      return card;
    }));
  }

  function actionButton(label, className, handler) {
    const button = node("button", className, label);
    button.type = "button";
    button.addEventListener("click", () => busy(button, handler).catch(() => {}));
    return button;
  }

  function operationCard(item) {
    const card = node("article", "card");
    const top = node("div", "section-head");
    const heading = node("h3", "", operationKindLabel(item.request_kind));
    top.append(heading, badge(stateLabels[item.state] || item.state, "warning"));
    card.append(top);
    card.append(node("p", "meta", `논문 ${formatNumber(item.target_count)}개 · ${formatDate(item.requested_at)} · ${shortId(item.request_id)}`));
    card.append(node("p", "", `요청 이유: ${item.reason_code}`));
    const actions = node("div", "actions");
    const detailTarget = node("div");
    actions.append(actionButton("대상·상태 확인", "secondary", async () => {
      const payload = await api(`/api/admin/operations/${item.request_kind}/${item.request_id}`);
      const detail = payload.request;
      const states = new Map((detail.target_states || []).map((value) => [value.paper_id, value]));
      const view = node("div", "card");
      view.append(node("h3", "", `대상 논문 ${formatNumber(detail.target_count)}개`));
      const list = node("div", "stack");
      for (const target of detail.targets || []) {
        const status = states.get(target.paper_id) || {};
        const row = node("p", "meta", `#${target.paper_id} · ${stateLabels[status.status] || status.status || "승인 대기"}`);
        if (target.rollback_to_version_id) {
          row.append(document.createTextNode(` · 이동 ${shortId(target.rollback_to_version_id)}`));
        }
        list.append(row);
      }
      view.append(list);
      replace(detailTarget, [view]);
    }));
    if (item.request_kind === "reprocess" && item.scope !== "selected") {
      card.append(node("p", "cost-note", "전체 재처리 요청은 이 화면에서 실행할 수 없습니다."));
    } else if (item.decision === "pending") {
      const approveCopy = item.request_kind === "rollback"
        ? "선택한 과거 버전으로 포인터를 바꾸는 작업을 승인할까요? 원본 PDF는 삭제되지 않습니다."
        : "선택한 논문을 다시 처리해 새 모바일 버전을 만드는 작업을 승인할까요?";
      actions.append(
        actionButton("승인", "", async () => {
          if (!window.confirm(approveCopy)) return;
          await api(`/api/admin/operations/${item.request_kind}/${item.request_id}/approve`, {
            method: "POST",
            body: { expected_revision: item.review_revision, confirm_execution: true },
          });
          toast("승인했습니다. 작업 대기열에 추가했습니다.");
          await loadOverview();
        }),
        actionButton("반려", "danger", async () => {
          if (!window.confirm("이 요청을 반려할까요? 반려 후에는 실행할 수 없습니다.")) return;
          await api(`/api/admin/operations/${item.request_kind}/${item.request_id}/reject`, {
            method: "POST",
            body: { expected_revision: item.review_revision, confirm_execution: false },
          });
          toast("요청을 반려했습니다.");
          await loadOverview();
        }),
      );
    }
    card.append(actions, detailTarget);
    return card;
  }

  function renderOperations(overview) {
    const pending = overview.pending_operations || [];
    replace(byId("pending-operations"), pending.length
      ? pending.map(operationCard)
      : [empty("지금 확인할 요청이 없습니다.")]);
    const recent = overview.recent_operations || [];
    replace(byId("recent-operations"), recent.length
      ? recent.map(operationCard)
      : [empty("최근 요청이 없습니다.")]);
  }

  function renderFailures(items) {
    replace(byId("operation-failures"), items.length ? items.map((item) => {
      const card = node("article", "card");
      const top = node("div", "section-head");
      top.append(node("h3", "", item.operation_kind), badge("실패", "danger"));
      card.append(top, node("p", "", `안전 코드: ${item.error_code || "unknown_failure"}`));
      card.append(node("p", "meta", `시도 ${formatNumber(item.attempt_count)}회 · ${formatDate(item.occurred_at)} · ${shortId(item.operation_id)}`));
      return card;
    }) : [empty("확인이 필요한 실패가 없습니다.")]);
  }

  function renderHealth(items) {
    replace(byId("worker-health"), items.length ? items.map((item) => {
      const card = node("article", "card");
      const healthy = item.status === "healthy";
      const top = node("div", "section-head");
      top.append(node("h3", "", item.service_id), badge(healthy ? "정상" : "확인 필요", healthy ? "" : "warning"));
      card.append(top, node("p", "meta", `${formatNumber(item.observed_worker_count)} / ${formatNumber(item.expected_worker_count)} 작업자 응답`));
      if (!healthy) card.append(node("p", "", (item.reason_codes || []).join(", ") || "health_unavailable"));
      return card;
    }) : [empty("등록된 작업자가 없습니다.")]);
  }

  function renderCost(cost) {
    const target = byId("cost-summary");
    if (!cost) {
      replace(target, [empty("사용량을 불러오지 못했습니다.")]);
      return;
    }
    const card = node("article", "card");
    card.append(node("h3", "", "AI 호출 누계"));
    const grid = node("div", "mini-grid");
    const values = [
      ["시도", formatNumber(cost.attempt_count)],
      ["전체 토큰", formatNumber(cost.total_tokens)],
      ["확인된 비용", `$${(Number(cost.known_cost_microusd || 0) / 1000000).toFixed(4)}`],
      ["비용 미확인", formatNumber(cost.unknown_cost_attempts)],
    ];
    for (const [label, value] of values) {
      const cell = node("div");
      cell.append(node("strong", "", value), node("span", "meta", label));
      grid.append(cell);
    }
    card.append(grid);
    replace(target, [card]);
  }

  function renderPaperChoices(papers) {
    state.papers = papers;
    const choices = byId("paper-choices");
    choices.setAttribute("aria-busy", "false");
    const rows = papers.map((paper) => {
      const label = node("label", "paper-choice");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "paper_id";
      input.value = String(paper.paper_id);
      const ready = paper.reader_status === "ready";
      input.disabled = !ready;
      const copy = node("span");
      copy.append(node("strong", "", paper.title));
      const readerLabel = ready ? "모바일 버전 준비됨" : `선택 불가 · ${paper.reader_status}`;
      copy.append(node("small", "", `#${paper.paper_id} · ${paper.category}${paper.publication_year ? ` · ${paper.publication_year}` : ""} · ${readerLabel}`));
      label.append(input, copy);
      if (!ready) label.setAttribute("aria-disabled", "true");
      label.dataset.search = `${paper.paper_id} ${paper.title} ${paper.category}`.toLocaleLowerCase("ko-KR");
      return label;
    });
    replace(choices, rows.length ? rows : [empty("선택할 수 있는 논문이 없습니다.")]);

    const rollback = byId("rollback-paper");
    const placeholder = node("option", "", "논문을 선택하세요");
    placeholder.value = "";
    replace(rollback, [placeholder, ...papers.map((paper) => {
      const option = node("option", "", `#${paper.paper_id} · ${paper.title}`);
      option.value = String(paper.paper_id);
      option.disabled = paper.reader_status !== "ready";
      return option;
    })]);
  }

  function auditCard(audit) {
    const card = node("article", "card");
    const top = node("div", "section-head");
    top.append(node("h3", "", audit.category), badge(audit.generation.requested ? stateLabels[audit.generation.status] || audit.generation.status : "분석 전", "warning"));
    card.append(top, node("p", "meta", `논문 ${formatNumber(audit.paper_count)}개 · 새 논문 ${formatNumber(audit.new_paper_count)}개`));
    card.append(node("p", "", `구조 검토 이유: ${(audit.reason_codes || []).join(", ")}`));
    if (!audit.generation.requested) {
      card.append(node("p", "cost-note", "DeepSeek Pro에는 카테고리, 내부 논문 ID, 논문 제목, 제한된 제목 기반 주제어만 전송합니다. PDF·본문·초록·개인 메모·파일명·폴더 경로·SHA는 전송하지 않으며 API 비용이 발생합니다."));
      const actions = node("div", "actions");
      actions.append(actionButton("DeepSeek Pro 분석 요청", "", async () => {
        if (!window.confirm("카테고리, 내부 논문 ID, 논문 제목, 제한된 제목 기반 주제어만 DeepSeek Pro에 보내며 API 비용이 발생합니다. PDF·본문·초록·개인 메모·파일명·폴더 경로·SHA는 보내지 않습니다. 계속할까요?")) return;
        await api(`/api/admin/taxonomy/audits/${audit.audit_id}/generation`, {
          method: "POST",
          body: { acknowledge_metadata_and_cost: true },
        });
        toast("폴더 구조 분석을 요청했습니다.");
        await loadOverview();
      }));
      card.append(actions);
    }
    return card;
  }

  async function showProposalDetail(proposal, container, button) {
    await busy(button, async () => {
      const payload = await api(`/api/admin/taxonomy/proposals/${proposal.proposal_id}`);
      const detail = payload.proposal;
      const view = node("div", "card");
      view.append(node("h3", "", `${detail.category} 제안 상세`));
      view.append(node("p", "", detail.rationale));
      const list = node("div", "stack");
      for (const subtheme of detail.subthemes || []) {
        const item = node("div", "card");
        item.append(node("strong", "", `${subtheme.name} · ${formatNumber(subtheme.assignment_count)}개`));
        item.append(node("p", "meta", subtheme.description));
        list.append(item);
      }
      view.append(list);
      const assignments = (detail.assignments || []).slice(0, 200);
      if (assignments.length) {
        const assignmentPanel = document.createElement("details");
        assignmentPanel.className = "panel";
        const summary = node("summary", "", `논문별 변경 ${formatNumber(detail.assignment_count)}개 확인`);
        const assignmentList = node("div", "panel-body paper-list");
        for (const assignment of assignments) {
          assignmentList.append(node(
            "p",
            "meta",
            `#${assignment.paper_id} · ${assignment.subtheme} · ${assignment.move_required ? "이동 예정" : "현재 위치 유지"}`,
          ));
        }
        if ((detail.assignments || []).length > assignments.length) {
          assignmentList.append(node("p", "empty", "목록이 길어 앞의 200개만 표시합니다."));
        }
        assignmentPanel.append(summary, assignmentList);
        view.append(assignmentPanel);
      }
      if (detail.review_status === "pending") {
        const actions = node("div", "actions");
        actions.append(
          actionButton("파일 이동 승인", "", async () => {
            if (!window.confirm("이 구조대로 파일 이동 작업을 대기열에 추가할까요? 실제 이동은 작업자가 검증 후 수행합니다.")) return;
            await api(`/api/admin/taxonomy/proposals/${proposal.proposal_id}/approve`, {
              method: "POST",
              body: { expected_revision: detail.review_revision, confirm_apply: true },
            });
            toast("폴더 구조 제안을 승인했습니다.");
            await loadOverview();
          }),
          actionButton("제안 반려", "danger", async () => {
            if (!window.confirm("이 폴더 구조 제안을 반려할까요?")) return;
            await api(`/api/admin/taxonomy/proposals/${proposal.proposal_id}/reject`, {
              method: "POST",
              body: { expected_revision: detail.review_revision, confirm_apply: false },
            });
            toast("폴더 구조 제안을 반려했습니다.");
            await loadOverview();
          }),
        );
        view.append(actions);
      }
      container.replaceWith(view);
    });
  }

  function proposalCard(proposal) {
    const card = node("article", "card");
    const top = node("div", "section-head");
    top.append(node("h3", "", proposal.category), badge("제안 검토 대기", "warning"));
    card.append(top, node("p", "meta", `${formatNumber(proposal.assignment_count)}개 논문 · ${formatNumber(proposal.subtheme_count)}개 주제`));
    const actions = node("div", "actions");
    const button = node("button", "secondary", "제안 내용 확인");
    button.type = "button";
    button.addEventListener("click", () => showProposalDetail(proposal, card, button).catch(() => {}));
    actions.append(button);
    card.append(actions);
    return card;
  }

  function renderTaxonomy(overview) {
    const audits = overview.taxonomy_audits || [];
    const proposals = overview.taxonomy_proposals || [];
    replace(byId("taxonomy-audits"), audits.length ? audits.map(auditCard) : [empty("새 분석이 필요한 폴더가 없습니다.")]);
    replace(byId("taxonomy-proposals"), proposals.length ? proposals.map(proposalCard) : [empty("검토할 폴더 구조 제안이 없습니다.")]);
  }

  async function loadOverview() {
    const refresh = byId("refresh");
    refresh.disabled = true;
    byId("updated-at").textContent = "불러오는 중…";
    try {
      const overview = await api("/api/admin/overview");
      state.overview = overview;
      renderSummary(overview);
      renderOperations(overview);
      renderFailures(overview.failures || []);
      renderHealth(overview.health || []);
      renderCost(overview.cost);
      renderPaperChoices(overview.papers || []);
      renderTaxonomy(overview);
      byId("updated-at").textContent = `${formatDate(overview.generated_at)} 기준`;
      if ((overview.section_errors || []).length) toast("일부 운영 정보를 불러오지 못했습니다.");
    } catch (_error) {
      byId("updated-at").textContent = "운영 정보를 불러오지 못했습니다.";
      toast("운영 정보를 불러오지 못했습니다.");
    } finally {
      refresh.disabled = false;
    }
  }

  function selectedPaperIds() {
    return Array.from(document.querySelectorAll('#paper-choices input[name="paper_id"]:checked'))
      .map((input) => Number(input.value));
  }

  function bindForms() {
    byId("paper-filter").addEventListener("input", (event) => {
      const query = event.target.value.trim().toLocaleLowerCase("ko-KR");
      for (const choice of byId("paper-choices").querySelectorAll(".paper-choice")) {
        choice.hidden = Boolean(query) && !choice.dataset.search.includes(query);
      }
    });

    byId("reprocess-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      const paperIds = selectedPaperIds();
      if (!paperIds.length) {
        toast("다시 처리할 논문을 먼저 선택하세요.");
        return;
      }
      if (!window.confirm(`선택한 ${paperIds.length}개 논문으로 재처리 요청을 만들까요? 생성 후 별도 승인이 필요합니다.`)) return;
      await busy(button, async () => {
        await api("/api/admin/operations/reprocess", {
          method: "POST",
          body: {
            paper_ids: paperIds,
            reason_code: byId("reprocess-reason").value,
            confirm_selected_reprocess: true,
          },
        });
        toast("재처리 요청을 만들었습니다. 아래에서 승인해 주세요.");
        await loadOverview();
      }).catch(() => {});
    });

    byId("rollback-paper").addEventListener("change", async (event) => {
      const paperId = Number(event.target.value);
      const target = byId("rollback-version");
      target.disabled = true;
      replace(target, [node("option", "", paperId ? "버전을 불러오는 중…" : "먼저 논문을 선택하세요")]);
      if (!paperId) return;
      try {
        const payload = await api(`/api/admin/papers/${paperId}/versions`);
        const options = (payload.rollback_version_ids || []).map((versionId) => {
          const option = node("option", "", shortId(versionId));
          option.value = versionId;
          return option;
        });
        replace(target, options.length ? options : [node("option", "", "되돌릴 과거 버전이 없습니다")]);
        target.disabled = !options.length;
      } catch (_error) {
        replace(target, [node("option", "", "버전을 불러오지 못했습니다")]);
      }
    });

    byId("rollback-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = event.submitter;
      const paperId = Number(byId("rollback-paper").value);
      const versionId = byId("rollback-version").value;
      if (!paperId || !versionId) {
        toast("논문과 되돌릴 버전을 선택하세요.");
        return;
      }
      if (!window.confirm("현재 모바일 버전을 선택한 과거 버전으로 되돌리는 요청을 만들까요? 생성 후 별도 승인이 필요합니다.")) return;
      await busy(button, async () => {
        await api("/api/admin/operations/rollback", {
          method: "POST",
          body: {
            targets: [{ paper_id: paperId, rollback_to_version_id: versionId }],
            reason_code: "manual_rollback",
            confirm_selected_rollback: true,
          },
        });
        toast("되돌리기 요청을 만들었습니다. 아래에서 승인해 주세요.");
        await loadOverview();
      }).catch(() => {});
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    byId("refresh").addEventListener("click", () => loadOverview());
    bindForms();
    loadOverview();
  });
})();
