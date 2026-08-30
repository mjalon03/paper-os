"use strict";

(() => {
  const article = document.querySelector("#article");
  if (!article) return;

  const paperId = Number(article.dataset.paperId);
  const fingerprint = article.dataset.articleFingerprint;
  const selectorVersion = Number(article.dataset.selectorVersion);
  const storageKey = `paper-reader-v2-${paperId}`;
  const actionHeaders = {
    "Content-Type": "application/json",
    "X-Paper-Action": "1",
  };

  const root = document.documentElement;
  const body = document.body;
  const progressBar = document.querySelector("#progress");
  const stateButton = document.querySelector("#stateButton");
  const statePanel = document.querySelector("#statePanel");
  const statusSelect = document.querySelector("#readingStatus");
  const favoriteInput = document.querySelector("#favorite");
  const prioritySelect = document.querySelector("#priority");
  const syncLabel = document.querySelector("#syncState");
  const annotationButton = document.querySelector("#annotationButton");
  const annotationPanel = document.querySelector("#annotationPanel");
  const annotationForm = document.querySelector("#annotationForm");
  const annotationList = document.querySelector("#annotationList");
  const annotationMessage = document.querySelector("#annotationMessage");
  const annotationPreview = document.querySelector("#annotationPreview");
  const selectionBar = document.querySelector("#selectionBar");
  const issueButton = document.querySelector("#issueButton");
  const issuePanel = document.querySelector("#issuePanel");
  const issueForm = document.querySelector("#issueForm");
  const issueList = document.querySelector("#issueList");
  const issueMessage = document.querySelector("#issueMessage");
  const statusLabels = {
    unread: "안 읽음",
    reading: "읽는 중",
    completed: "완료",
    on_hold: "보류",
    rereading: "다시 읽기",
  };

  const initialState = {
    paper_id: paperId,
    status: "unread",
    progress: 0,
    anchor: null,
    article_fingerprint: null,
    favorite: false,
    priority: 0,
    revision: 0,
  };
  let state = { ...initialState };
  let loaded = false;
  let saving = false;
  let pending = {};
  let saveTimer = null;
  let selectedAnchor = null;
  let annotations = [];
  let issues = [];

  function cleanText(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }

  function randomUuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const value = [...bytes].map((item) => item.toString(16).padStart(2, "0"));
    return `${value.slice(0, 4).join("")}-${value.slice(4, 6).join("")}-${value.slice(6, 8).join("")}-${value.slice(8, 10).join("")}-${value.slice(10).join("")}`;
  }

  async function jsonRequest(url, options = {}) {
    const response = await fetch(url, {
      credentials: "same-origin",
      ...options,
      headers: options.method && options.method !== "GET" ? actionHeaders : undefined,
    });
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error(`request failed: ${response.status}`);
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  }

  function localState() {
    try {
      const value = JSON.parse(localStorage.getItem(`${storageKey}-state`) || "null");
      return value && typeof value === "object" ? value : null;
    } catch {
      return null;
    }
  }

  function remember() {
    localStorage.setItem(
      `${storageKey}-state`,
      JSON.stringify({
        status: state.status,
        progress: state.progress,
        anchor: state.anchor,
        article_fingerprint: fingerprint,
        favorite: state.favorite,
        priority: state.priority,
      }),
    );
  }

  function updateStateUi() {
    stateButton.textContent = statusLabels[state.status] || "상태";
    statusSelect.value = state.status;
    favoriteInput.checked = Boolean(state.favorite);
    prioritySelect.value = String(state.priority || 0);
  }

  function activeNode() {
    const nodes = [...document.querySelectorAll("[data-node-id]")];
    const line = innerHeight * 0.35;
    return (
      [...nodes]
        .reverse()
        .find(
          (node) =>
            node.getBoundingClientRect().top <= line &&
            node.getBoundingClientRect().bottom > 0,
        ) ||
      nodes[0] ||
      null
    );
  }

  function baseAnchor(node) {
    const anchor = {
      selector_version: selectorVersion,
      article_fingerprint: fingerprint,
      scope: node.closest(".abstract") ? "abstract" : "body",
      section_id: node.dataset.sectionId,
      node_id: node.dataset.nodeId,
    };
    if (node.dataset.page) anchor.page = Number(node.dataset.page);
    return anchor;
  }

  function currentAnchor() {
    const node = activeNode();
    if (!node) return null;
    const anchor = baseAnchor(node);
    const exact = cleanText(node.textContent).slice(0, 240);
    if (exact) {
      Object.assign(anchor, {
        start_utf16: 0,
        end_utf16: exact.length,
        exact_quote: exact,
        prefix: "",
        suffix: "",
      });
    }
    return anchor;
  }

  function quoteTarget(anchor) {
    if (!anchor || !anchor.exact_quote) return null;
    const all = [...document.querySelectorAll("[data-node-id]")];
    const sameSection = anchor.section_id
      ? all.filter((node) => node.dataset.sectionId === anchor.section_id)
      : [];
    const pool = sameSection.length ? sameSection : all;
    const matches = pool.filter((node) =>
      cleanText(node.textContent).includes(anchor.exact_quote),
    );
    return matches.length === 1 ? matches[0] : null;
  }

  function restoreReadingPosition(saved) {
    if (scrollY > 40) return;
    let target = null;
    if (saved?.anchor?.node_id) {
      target = document.querySelector(
        `[data-node-id="${CSS.escape(saved.anchor.node_id)}"]`,
      );
    }
    if (!target) target = quoteTarget(saved?.anchor);
    if (target) {
      target.scrollIntoView({ block: "start" });
      return;
    }
    const progress = Number(saved?.progress || 0);
    if (progress > 0) {
      scrollTo(
        0,
        Math.max(0, (document.documentElement.scrollHeight - innerHeight) * progress),
      );
      return;
    }
    const oldPosition = Number(localStorage.getItem(`${storageKey}-scroll`) || 0);
    if (oldPosition > 40) scrollTo(0, oldPosition);
  }

  async function sendPending(keepalive = false, retry = true) {
    if (saving || !loaded || !Object.keys(pending).length) return;
    saving = true;
    const patch = { ...pending };
    pending = {};
    syncLabel.textContent = "저장 중…";
    try {
      const response = await fetch(`/api/papers/${paperId}/reading-state`, {
        method: "PATCH",
        credentials: "same-origin",
        keepalive,
        headers: actionHeaders,
        body: JSON.stringify({
          expected_revision: Number(state.revision || 0),
          ...patch,
        }),
      });
      const payload = await response.json().catch(() => ({}));
      if (response.status === 409 && retry) {
        state = { ...initialState, ...(payload.detail?.current || {}) };
        pending = { ...patch, ...pending };
        saving = false;
        return sendPending(keepalive, false);
      }
      if (!response.ok) throw new Error("save failed");
      state = { ...state, ...payload };
      remember();
      updateStateUi();
      syncLabel.textContent = "서버에 저장됨";
    } catch {
      pending = { ...patch, ...pending };
      remember();
      syncLabel.textContent = "오프라인 저장됨";
    } finally {
      saving = false;
    }
  }

  function queueSave(patch, immediate = false) {
    state = { ...state, ...patch, article_fingerprint: fingerprint };
    pending = { ...pending, ...patch, article_fingerprint: fingerprint };
    remember();
    updateStateUi();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => sendPending(false, true), immediate ? 0 : 1500);
  }

  async function loadReadingState() {
    const fallback = localState();
    if (fallback) state = { ...state, ...fallback };
    updateStateUi();
    try {
      const saved = await jsonRequest(`/api/papers/${paperId}/reading-state`);
      state = { ...initialState, ...saved };
      syncLabel.textContent = "서버와 동기화됨";
    } catch {
      syncLabel.textContent = "오프라인 상태";
    }
    loaded = true;
    updateStateUi();
    progressBar.style.width = `${Number(state.progress || 0) * 100}%`;
    restoreReadingPosition(state.anchor || state.progress ? state : fallback);
    if (Number(state.progress || 0) > 0.01) {
      document.querySelector("#start").textContent = "이어 읽기";
    }
    if (Object.keys(pending).length) sendPending();
  }

  function updateProgress(persist = true) {
    const maximum = document.documentElement.scrollHeight - innerHeight;
    const progress = maximum > 0 ? scrollY / maximum : 0;
    progressBar.style.width = `${progress * 100}%`;
    localStorage.setItem(`${storageKey}-scroll`, String(scrollY));
    if (!persist) return;
    const patch = { progress, anchor: currentAnchor() };
    if (state.status === "unread" && progress > 0.01) patch.status = "reading";
    if (state.status === "reading" && progress > 0.985) patch.status = "completed";
    queueSave(patch);
  }

  function elementForContainer(container) {
    return container.nodeType === Node.ELEMENT_NODE
      ? container
      : container.parentElement;
  }

  function captureSelection() {
    const selection = getSelection();
    if (!selection || selection.rangeCount !== 1 || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    const startNode = elementForContainer(range.startContainer)?.closest("[data-node-id]");
    const endNode = elementForContainer(range.endContainer)?.closest("[data-node-id]");
    if (!startNode || startNode !== endNode) return null;
    if (!["paragraph", "list", "equation"].includes(startNode.dataset.kind)) return null;
    const exact = range.toString();
    if (!exact.trim()) return null;
    const before = document.createRange();
    before.selectNodeContents(startNode);
    before.setEnd(range.startContainer, range.startOffset);
    const start = before.toString().length;
    const end = start + exact.length;
    const text = startNode.textContent || "";
    if (text.slice(start, end) !== exact) return null;
    return {
      ...baseAnchor(startNode),
      start_utf16: start,
      end_utf16: end,
      exact_quote: exact,
      prefix: text.slice(Math.max(0, start - 120), start),
      suffix: text.slice(end, end + 120),
    };
  }

  function updateSelectionBar() {
    const anchor = captureSelection();
    selectionBar.hidden = !anchor;
    selectionBar._anchor = anchor;
  }

  function textPoint(node, offset) {
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let consumed = 0;
    while (walker.nextNode()) {
      const textNode = walker.currentNode;
      const next = consumed + textNode.data.length;
      if (offset <= next) return [textNode, Math.max(0, offset - consumed)];
      consumed = next;
    }
    return null;
  }

  function annotationTarget(annotation) {
    const anchor = annotation.anchor;
    if (!anchor?.node_id || !anchor.exact_quote) return null;
    let node = document.querySelector(
      `[data-node-id="${CSS.escape(anchor.node_id)}"]`,
    );
    if (node) {
      const text = node.textContent || "";
      if (
        text.slice(anchor.start_utf16, anchor.end_utf16) === anchor.exact_quote
      ) {
        return {
          node,
          start: anchor.start_utf16,
          end: anchor.end_utf16,
          remapped: annotation.article_fingerprint !== fingerprint,
        };
      }
      const matches = [...text.matchAll(new RegExp(escapeRegex(anchor.exact_quote), "g"))];
      if (matches.length === 1) {
        return {
          node,
          start: matches[0].index,
          end: matches[0].index + anchor.exact_quote.length,
          remapped: true,
        };
      }
    }
    const candidates = [...document.querySelectorAll("[data-node-id]")].filter(
      (candidate) => (candidate.textContent || "").includes(anchor.exact_quote),
    );
    if (candidates.length !== 1) return null;
    node = candidates[0];
    const start = (node.textContent || "").indexOf(anchor.exact_quote);
    return {
      node,
      start,
      end: start + anchor.exact_quote.length,
      remapped: true,
    };
  }

  function escapeRegex(value) {
    return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function applyAnnotation(annotation) {
    if (document.querySelector(`[data-annotation-id="${CSS.escape(annotation.id)}"]`)) {
      return "applied";
    }
    const target = annotationTarget(annotation);
    if (!target) return "needs_review";
    if (annotation.kind === "highlight") {
      const start = textPoint(target.node, target.start);
      const end = textPoint(target.node, target.end);
      if (!start || !end) return "needs_review";
      try {
        const range = document.createRange();
        range.setStart(start[0], start[1]);
        range.setEnd(end[0], end[1]);
        const mark = document.createElement("mark");
        mark.dataset.annotationId = annotation.id;
        if (target.remapped) mark.classList.add("remapped");
        range.surroundContents(mark);
      } catch {
        return "needs_review";
      }
    } else {
      const pin = document.createElement("button");
      pin.type = "button";
      pin.className = `annotation-pin${target.remapped ? " remapped" : ""}`;
      pin.dataset.annotationId = annotation.id;
      pin.textContent = annotation.kind === "question" ? "?" : "✎";
      pin.onclick = () => {
        annotationPanel.showModal();
        document.querySelector(`[data-annotation-card="${annotation.id}"]`)?.scrollIntoView();
      };
      target.node.insertAdjacentElement("afterend", pin);
    }
    return target.remapped ? "remapped" : "applied";
  }

  function annotationCard(annotation, placement) {
    const card = document.createElement("article");
    card.className = "annotation-card";
    card.dataset.annotationCard = annotation.id;
    const label = { highlight: "하이라이트", note: "메모", question: "질문" }[
      annotation.kind
    ];
    const warning = placement === "needs_review" ? " · 위치 검토 필요" : "";
    const quote = document.createElement("blockquote");
    quote.textContent = annotation.anchor?.exact_quote || "";
    const heading = document.createElement("strong");
    heading.textContent = `${label}${warning}`;
    const bodyText = document.createElement("p");
    bodyText.textContent = annotation.body || annotation.category || "";
    const sharing = document.createElement("p");
    sharing.textContent = annotation.ai_share_allowed
      ? "AI 답변에 사용 허용됨"
      : "AI 답변에는 사용하지 않음";
    const actions = document.createElement("div");
    actions.className = "card-actions";
    if (annotation.kind === "question" && annotation.question_status === "open") {
      const answer = document.createElement("button");
      answer.type = "button";
      answer.textContent = "답변됨";
      answer.onclick = () => updateAnnotation(annotation, { question_status: "answered" });
      actions.append(answer);
    }
    const share = document.createElement("button");
    share.type = "button";
    share.textContent = annotation.ai_share_allowed ? "AI 공유 끄기" : "AI 공유 허용";
    share.onclick = () =>
      updateAnnotation(annotation, { ai_share_allowed: !annotation.ai_share_allowed });
    actions.append(share);
    const trash = document.createElement("button");
    trash.type = "button";
    trash.textContent = "휴지통";
    trash.onclick = () => trashAnnotation(annotation);
    actions.append(trash);
    card.append(heading, quote, bodyText, sharing, actions);
    return card;
  }

  function renderAnnotations() {
    annotationList.replaceChildren();
    let needsReview = 0;
    for (const annotation of annotations) {
      const placement = applyAnnotation(annotation);
      if (placement === "needs_review") needsReview += 1;
      annotationList.append(annotationCard(annotation, placement));
    }
    annotationButton.textContent = annotations.length ? `메모 ${annotations.length}` : "메모";
    annotationMessage.textContent = needsReview
      ? `${needsReview}개의 위치는 자동 연결하지 않았습니다.`
      : "저장된 메모 위치를 확인했습니다.";
  }

  async function loadAnnotations() {
    try {
      const payload = await jsonRequest(`/api/papers/${paperId}/annotations`);
      annotations = payload.annotations || [];
      renderAnnotations();
    } catch {
      annotationMessage.textContent = "메모를 불러오지 못했습니다.";
    }
  }

  function openAnnotationForm(anchor) {
    selectedAnchor = anchor;
    annotationPreview.textContent = anchor?.exact_quote || "본문을 먼저 선택하세요.";
    annotationPanel.showModal();
  }

  async function createAnnotationFromForm(event) {
    event.preventDefault();
    if (!selectedAnchor) {
      annotationMessage.textContent = "한 문단 안에서 저장할 문구를 선택하세요.";
      return;
    }
    const data = new FormData(annotationForm);
    const kind = String(data.get("kind"));
    const bodyText = String(data.get("body") || "").trim();
    const category = String(data.get("category") || "").trim();
    const payload = {
      id: randomUuid(),
      expected_revision: 0,
      kind,
      anchor: selectedAnchor,
      article_fingerprint: fingerprint,
      body: bodyText || null,
      category: category || null,
      question_status: kind === "question" ? "open" : null,
      ai_share_allowed: data.get("ai_share_allowed") === "on",
    };
    annotationMessage.textContent = "저장 중…";
    try {
      const created = await jsonRequest(`/api/papers/${paperId}/annotations`, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      annotations.unshift(created);
      selectedAnchor = null;
      annotationForm.reset();
      getSelection()?.removeAllRanges();
      selectionBar.hidden = true;
      renderAnnotations();
    } catch {
      annotationMessage.textContent = "메모를 저장하지 못했습니다.";
    }
  }

  async function updateAnnotation(annotation, patch) {
    try {
      const updated = await jsonRequest(`/api/annotations/${annotation.id}`, {
        method: "PATCH",
        body: JSON.stringify({ expected_revision: annotation.revision, ...patch }),
      });
      annotations = annotations.map((item) => (item.id === updated.id ? updated : item));
      renderAnnotations();
    } catch (error) {
      if (error.status === 409) await loadAnnotations();
      else annotationMessage.textContent = "메모 상태를 변경하지 못했습니다.";
    }
  }

  async function trashAnnotation(annotation) {
    try {
      await jsonRequest(`/api/annotations/${annotation.id}/trash`, {
        method: "POST",
        body: JSON.stringify({ expected_revision: annotation.revision }),
      });
      const marker = document.querySelector(
        `[data-annotation-id="${CSS.escape(annotation.id)}"]`,
      );
      if (marker?.tagName === "MARK") marker.replaceWith(...marker.childNodes);
      else marker?.remove();
      annotations = annotations.filter((item) => item.id !== annotation.id);
      renderAnnotations();
    } catch (error) {
      if (error.status === 409) await loadAnnotations();
      else annotationMessage.textContent = "메모를 휴지통으로 옮기지 못했습니다.";
    }
  }

  function issueCard(issue) {
    const card = document.createElement("article");
    card.className = "issue-card";
    const heading = document.createElement("strong");
    heading.textContent = `${issue.kind} · ${issue.status}`;
    const description = document.createElement("p");
    description.textContent = issue.description || "설명 없음";
    card.append(heading, description);
    if (issue.status === "open") {
      const actions = document.createElement("div");
      actions.className = "card-actions";
      for (const [label, status] of [
        ["해결", "resolved"],
        ["닫기", "dismissed"],
      ]) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.onclick = () => updateIssue(issue, status);
        actions.append(button);
      }
      card.append(actions);
    }
    return card;
  }

  function renderIssues() {
    issueList.replaceChildren(...issues.map(issueCard));
    const openCount = issues.filter((issue) => issue.status === "open").length;
    issueButton.textContent = openCount ? `신고 ${openCount}` : "신고";
  }

  async function loadIssues() {
    try {
      const payload = await jsonRequest(`/api/papers/${paperId}/issues`);
      issues = payload.issues || [];
      renderIssues();
    } catch {
      issueMessage.textContent = "신고 내역을 불러오지 못했습니다.";
    }
  }

  async function createIssueFromForm(event) {
    event.preventDefault();
    const anchor = currentAnchor();
    if (!anchor) {
      issueMessage.textContent = "본문 위치를 찾을 수 없습니다.";
      return;
    }
    const data = new FormData(issueForm);
    issueMessage.textContent = "저장 중…";
    try {
      const created = await jsonRequest(`/api/papers/${paperId}/issues`, {
        method: "POST",
        body: JSON.stringify({
          id: randomUuid(),
          kind: String(data.get("kind")),
          description: String(data.get("description") || "").trim() || null,
          anchor,
          article_fingerprint: fingerprint,
        }),
      });
      issues.unshift(created);
      issueForm.reset();
      issueMessage.textContent = "현재 위치와 함께 저장했습니다.";
      renderIssues();
    } catch {
      issueMessage.textContent = "신고를 저장하지 못했습니다.";
    }
  }

  async function updateIssue(issue, newStatus) {
    try {
      const updated = await jsonRequest(`/api/issues/${issue.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          expected_revision: issue.revision,
          status: newStatus,
        }),
      });
      issues = issues.map((item) => (item.id === updated.id ? updated : item));
      renderIssues();
    } catch (error) {
      if (error.status === 409) await loadIssues();
      else issueMessage.textContent = "신고 상태를 변경하지 못했습니다.";
    }
  }

  let size = Number(localStorage.getItem(`${storageKey}-size`) || 1);
  root.style.setProperty("--scale", size);
  document.querySelector("#size").onclick = () => {
    size = size >= 1.3 ? 0.9 : size + 0.1;
    root.style.setProperty("--scale", size);
    localStorage.setItem(`${storageKey}-size`, String(size));
  };
  const themes = ["system", "light", "dark"];
  let theme = localStorage.getItem(`${storageKey}-theme`) || "system";
  function paintTheme() {
    body.classList.remove("theme-light", "theme-dark");
    if (theme !== "system") body.classList.add(`theme-${theme}`);
  }
  paintTheme();
  document.querySelector("#theme").onclick = () => {
    theme = themes[(themes.indexOf(theme) + 1) % themes.length];
    localStorage.setItem(`${storageKey}-theme`, theme);
    paintTheme();
  };

  const toc = document.querySelector("#toc");
  document.querySelector("#tocButton").onclick = () => {
    toc.open = !toc.open;
    toc.scrollIntoView({ block: "start" });
  };
  stateButton.onclick = () => statePanel.showModal();
  statusSelect.onchange = () => queueSave({ status: statusSelect.value }, true);
  favoriteInput.onchange = () => queueSave({ favorite: favoriteInput.checked }, true);
  prioritySelect.onchange = () => queueSave({ priority: Number(prioritySelect.value) }, true);
  annotationButton.onclick = () => openAnnotationForm(captureSelection());
  selectionBar.onclick = () => openAnnotationForm(selectionBar._anchor);
  issueButton.onclick = () => issuePanel.showModal();
  annotationForm.onsubmit = createAnnotationFromForm;
  issueForm.onsubmit = createIssueFromForm;
  document.querySelectorAll("[data-close-dialog]").forEach((button) => {
    button.onclick = () => button.closest("dialog").close();
  });

  let ticking = false;
  addEventListener(
    "scroll",
    () => {
      if (!ticking) {
        requestAnimationFrame(() => {
          updateProgress(true);
          ticking = false;
        });
        ticking = true;
      }
    },
    { passive: true },
  );
  document.addEventListener("selectionchange", updateSelectionBar);

  const current = document.querySelector("#current");
  const sections = [...document.querySelectorAll(".article-section")];
  const observer = new IntersectionObserver(
    (entries) => {
      entries
        .filter((entry) => entry.isIntersecting)
        .forEach((entry) => {
          current.textContent = entry.target.dataset.section;
        });
    },
    { rootMargin: "-20% 0px -70% 0px" },
  );
  sections.forEach((section) => observer.observe(section));

  const zoom = document.querySelector("#zoom");
  const zoomImage = zoom.querySelector("img");
  document.querySelectorAll(".figure-open img").forEach((image) => {
    image.parentElement.onclick = () => {
      zoomImage.src = image.src;
      zoomImage.alt = image.alt;
      zoom.showModal();
    };
  });
  zoom.querySelector("button").onclick = () => zoom.close();
  zoom.onclick = (event) => {
    if (event.target === zoom) zoom.close();
  };

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") sendPending(true, true);
  });
  updateStateUi();
  loadReadingState();
  loadAnnotations();
  loadIssues();
  updateProgress(false);
})();
