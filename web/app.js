const S = {
  tab: "flow",
  nodeTypes: [],
  elementTypes: [],
  elementDefaults: {},
  imageSizePresets: [],
  elements: [],
  flows: [],
  flow: null,
  keyStatus: {},
  runs: [],
  selected: null,
  linking: null,
  dragging: null,
  running: false,
  zoom: 1,
  saveState: "",
  elementId: null,
  elementQuery: "",
  elementTypeFilter: "",
  elementGroupFilter: "",
  elementTagFilter: "",
  elementStatusFilter: "",
  elementChecked: [],
  elementDraft: null,
  flowQuery: "",
  flowGroupFilter: "",
  flowTagFilter: "",
  flowStatusFilter: "",
  flowSort: "updated-desc",
  flowChecked: [],
  flowDraft: null,
  flowCopyIds: [],
  flowCopyBusy: false,
  batchRunIds: [],
  batchRunBusy: false,
  batches: [],
  executionConfig: { flowConcurrency: 3, promptConcurrency: 6, imageConcurrency: 6 },
  executionDraft: null,
  executionSaving: false,
  lastTrace: null,
  results: [],
  resultChecked: [],
  resultQuery: "",
  resultGroupFilter: "",
  resultTagFilter: "",
  resultStatusFilter: "",
  runChecked: [],
  runQuery: "",
  runStatusFilter: "",
  runFlowFilter: "",
  runModeFilter: "",
  runDraft: null,
  referenceZipBusy: false,
  tableImport: "",
  resultDraft: null,
  editing: false,
};

let saveChain = Promise.resolve();
let saveTimer = 0;
let elementTimer = 0;
let confirmationPending = false;

const COMMON_HINTS = [
  "theme",
  "theme.text",
  "theme.domain.name",
  "theme.domain.tone",
  "prompt",
  "prompt.text",
  "titles",
  "titles.items",
  "titles.items.0.text",
  "primaryTitle",
  "articles",
  "articles.items.0.title",
  "articles.items.0.body",
  "articleExcerpt",
  "images",
  "images.items.0.path",
  "imageSize.width",
  "imageSize.height",
  "imageSize.ratio",
  "images.items.0.actualSize",
  "referenceUsed.name",
  "referenceUsed.note",
  "referencesUsed",
  "referencesUsed.0.name",
  "referencesUsed.0.note",
  "referenceNames",
  "referenceNotes",
  "title",
];

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function newId(kind) {
  return kind + "_" + crypto.randomUUID();
}

async function api(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(url, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "请求失败 " + response.status);
  return data;
}

function typeInfo(type) {
  return S.nodeTypes.find((item) => item.type === type) || {
    label: type, product: [], forward: [], accepts: [], hints: [], elementTypes: [], blurb: "", group: "",
  };
}

function elementTypeLabel(type) {
  const found = S.elementTypes.find((item) => item.type === type);
  return found ? found.label : type;
}

const ADD_FORMS = [
  ["reference_image", "添加参考图"],
  ["reference_article", "添加文章"],
  ["prompt", "添加提示词"],
  ["rule", "添加规则"],
  ["model_profile", "添加API配置"],
];

function elementFormTitle(element) {
  const named = ADD_FORMS.find((item) => item[0] === element.type);
  if (named) return element.id ? named[1].replace("添加", "编辑") : named[1];
  const label = elementTypeLabel(element.type);
  return (element.id ? "编辑" : "添加") + label;
}

function focusSoon(selector) {
  const el = document.querySelector(selector);
  if (!el) return;
  requestAnimationFrame(() => {
    if (!el.isConnected || el.closest(".hidden") || document.activeElement === el) return;
    el.focus({ preventScroll: true });
    if (typeof el.setSelectionRange === "function" && el.type !== "number") {
      const end = el.value.length;
      el.setSelectionRange(end, end);
    }
  });
}

function confirmAction(message) {
  if (confirmationPending) return Promise.resolve(false);
  const dialog = document.getElementById("confirm-dialog");
  const previousFocus = document.activeElement;
  document.getElementById("confirm-message").textContent = message;
  dialog.returnValue = "cancel";
  confirmationPending = true;
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => {
      confirmationPending = false;
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
      resolve(dialog.returnValue === "confirm");
    }, { once: true });
    dialog.showModal();
  });
}

function findNode(id) {
  return (S.flow?.graph.nodes || []).find((node) => node.id === id) || null;
}

function findEdge(id) {
  return (S.flow?.graph.edges || []).find((edge) => edge.id === id) || null;
}

function currentElement() {
  return S.elements.find((item) => item.id === S.elementId) || null;
}

function activeRecord() {
  return S.elementDraft || currentElement();
}

const PAGES = {
  flow: ["FLOWS", "流程", "点一条流程进入编辑。"],
  elements: ["LIBRARY", "元素库", "用上方按钮添加参考图、文章、提示词、规则或 API 配置。点一行可以再编辑。"],
  runs: ["RUNS", "运行记录", "查看每次执行的节点结果、文件夹和 ZIP。"],
  results: ["RESULTS", "结果库", "成功保存的结果图。分组和标签来自本次携带的参考图，编号是 UUID。"],
  guide: ["GUIDE", "使用说明", "携带、规则、定时，以及案例流程怎么读。"],
  execution: ["SETTINGS", "执行配置", "统一设置流程、提示词和生图的并发数量。"],
};

function setBanner(text, kind) {
  const root = document.getElementById("toast-root");
  if (!root) return;
  root.replaceChildren();
  if (!text) return;
  const toast = document.createElement("div");
  toast.className = "toast" + (kind === "bad" ? " error" : "");
  toast.textContent = text;
  root.appendChild(toast);
  if (kind === "ok") setTimeout(() => { if (toast.isConnected) toast.remove(); }, 3600);
}

function setSaveLabel() {
  const el = document.getElementById("save-state");
  if (el) el.textContent = S.saveState || "本地数据库";
}

function pageMeta() {
  if (S.tab === "flow" && S.editing && S.flow) {
    return ["EDIT", S.flow.name || "未命名流程", "从左侧拖入组件。从一个节点拖到下一个节点建立依赖，再选择要携带的字段。"];
  }
  if (S.tab === "elements") {
    const label = elementTypeLabel(S.elementTypeFilter) || "元素库";
    return ["LIBRARY", label, "右侧只显示这一类。检索、分组和标签筛选也只在这一类里。"];
  }
  return PAGES[S.tab] || PAGES.flow;
}

function renderElementNav() {
  const root = document.getElementById("element-nav");
  if (!root) return;
  root.classList.toggle("open", S.tab === "elements" && S.elementNavOpen !== false);
  root.innerHTML = S.elementTypes.map((item) => {
    const count = S.elements.filter((element) => element.type === item.type).length;
    const on = S.tab === "elements" && S.elementTypeFilter === item.type;
    return `<button type="button" data-element-type="${esc(item.type)}" class="${on ? "active" : ""}">${esc(item.label)} <small>${count}</small></button>`;
  }).join("");
}

function ensureElementModule() {
  if (S.elementTypeFilter && S.elementTypes.some((item) => item.type === S.elementTypeFilter)) return;
  S.elementTypeFilter = S.elementTypes[0]?.type || "";
}

function renderChrome() {
  const page = pageMeta();
  document.querySelectorAll("nav [data-tab]").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === S.tab && !button.dataset.elementType);
  });
  renderElementNav();
  document.getElementById("crumb-page").textContent = page[1];
  document.getElementById("page-eyebrow").textContent = page[0];
  document.getElementById("page-title").textContent = page[1];
  document.getElementById("page-description").textContent = page[2];
  const pill = document.getElementById("provider-pill");
  if (pill) {
    const ready = Object.values(S.keyStatus).some((item) => item.hasKey);
    pill.textContent = ready ? "模型密钥已保存" : "未配置模型 · 本地草稿";
  }
}

function nextLabel(schedule) {
  if (!schedule?.enabled || !schedule.nextRunAt) return "定时未启用";
  const date = new Date(schedule.nextRunAt);
  if (Number.isNaN(date.getTime())) return "时间未定";
  return "下次执行 " + date.toLocaleString();
}

async function load(preferId) {
  const data = await api("GET", "/api/bootstrap");
  S.nodeTypes = data.nodeTypes;
  S.elementTypes = data.elementTypes;
  S.elementDefaults = data.elementDefaults;
  S.imageSizePresets = data.imageSizePresets || [];
  S.elements = data.elements;
  S.flows = data.flows;
  S.keyStatus = data.keyStatus || {};
  S.executionConfig = data.executionConfig || S.executionConfig;
  document.getElementById("version").textContent = data.version || "";
  const wanted = preferId || S.flow?.id || "flow-wallpaper-article";
  S.flow = S.flows.find((item) => item.id === wanted) || S.flows[0] || null;
  if (!S.elementId || !currentElement()) S.elementId = S.elements[0]?.id || null;
  if (S.flow) {
    const runs = await api("GET", "/api/runs?flowId=" + encodeURIComponent(S.flow.id) + "&limit=1");
    S.lastTrace = runs.runs[0]?.trace || null;
  }
  render();
  refreshBatchRuns().catch(() => {});
}

function touch() {
  S.saveState = "待保存";
  setSaveLabel();
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => {
    saveChain = saveChain.then(() => persist()).catch((error) => setBanner(error.message, "bad"));
  }, 350);
}

function persistNow() {
  clearTimeout(saveTimer);
  const job = saveChain.then(() => (S.flow ? persist() : null));
  saveChain = job.catch(() => {});
  return job;
}

async function persist() {
  if (!S.flow) return;
  const id = S.flow.id;
  const body = {
    name: S.flow.name,
    description: S.flow.description || "",
    graph: S.flow.graph,
    schedule: S.flow.schedule,
    group: S.flow.group || "",
    tags: S.flow.tags || [],
    status: S.flow.status || "启用",
    fields: S.flow.fields || [],
  };
  const sentSchedule = JSON.stringify(body.schedule);
  S.saveState = "保存中";
  setSaveLabel();
  const saved = await api("PUT", "/api/flows/" + encodeURIComponent(id), body);
  if (S.flow && S.flow.id === id && JSON.stringify(S.flow.schedule) === sentSchedule) {
    S.flow.schedule = saved.schedule;
  }
  const listed = S.flows.find((item) => item.id === id);
  if (listed) listed.name = saved.name;
  S.saveState = "已保存";
  setSaveLabel();
  const next = document.getElementById("next-run");
  if (next && S.flow) next.textContent = nextLabel(S.flow.schedule);
}

function touchElement() {
  clearTimeout(elementTimer);
  elementTimer = setTimeout(() => {
    persistElement().catch((error) => setBanner(error.message, "bad"));
  }, 350);
}

function flushElement() {
  clearTimeout(elementTimer);
  return persistElement();
}

async function persistElement() {
  const element = currentElement();
  if (!element) return;
  const sentName = element.name;
  const saved = await api("PUT", "/api/elements/" + encodeURIComponent(element.id), {
    type: element.type,
    name: element.name,
    payload: element.payload,
  });
  if (element.name === sentName) element.name = saved.name;
  element.payload = saved.payload;
  element.updatedAt = saved.updatedAt;
  renderElementTable();
}

function defaultMappings(srcType, dstType) {
  const src = typeInfo(srcType);
  const dst = typeInfo(dstType);
  const available = new Set([...(src.product || []), ...(src.forward || [])]);
  const maps = (dst.accepts || [])
    .filter((name) => available.has(name))
    .map((name) => ({ source: name, target: name, op: "copy" }));
  if (!maps.length && src.product?.[0]) {
    maps.push({ source: src.product[0], target: src.product[0], op: "copy" });
  }
  return maps;
}

function boardSize() {
  let width = 960;
  let height = 640;
  for (const node of S.flow?.graph.nodes || []) {
    width = Math.max(width, (node.x || 0) + 300);
    height = Math.max(height, (node.y || 0) + 220);
  }
  return { width, height };
}

function render() {
  if (S.tab === "elements") ensureElementModule();
  renderChrome();
  renderTop();
  const main = document.getElementById("main");
  const editingCanvas = S.tab === "flow" && S.editing && S.flow;
  main.className = "stage " + (editingCanvas ? "flow" : "scroll");
  if (S.tab === "execution") { renderExecutionConfig(); return; }
  if (S.tab === "guide") {
    main.innerHTML = document.getElementById("guide-template").innerHTML;
    return;
  }
  if (S.tab === "elements") {
    ensureElementModule();
    main.innerHTML = '<div class="element-page"><div class="list-toolbar" id="element-toolbar"></div><div class="table-panel flow-list" id="element-table"></div></div>';
    renderElementToolbar();
    renderElementTable();
    return;
  }
  if (S.tab === "runs") {
    main.innerHTML = '<div class="runs" id="runs"></div>';
    renderRuns();
    return;
  }
  if (S.tab === "results") {
    main.innerHTML = '<div class="runs" id="results"></div>';
    renderResults();
    return;
  }
  if (!editingCanvas) {
    main.innerHTML = '<div class="element-page"><div id="batch-status-list" class="batch-status-list" aria-live="polite"></div><div class="list-toolbar" id="flow-toolbar"></div><div class="table-panel flow-list" id="flow-table"></div></div>';
    renderFlowToolbar();
    renderFlowList();
    renderBatchStatuses();
    return;
  }
  main.innerHTML = `
    <div class="workspace">
      <aside class="palette" id="palette"></aside>
      <div class="canvas-col">
        <div class="canvas-toolbar">
          <button type="button" class="btn primary" data-act="format-dag">一键布局</button>
          <button type="button" class="btn" data-act="zoom-out">缩小</button>
          <span id="zoom-label">100%</span>
          <button type="button" class="btn" data-act="zoom-in">放大</button>
          <button type="button" class="btn" data-act="zoom-reset">100%</button>
          <span class="hint">拖动画布空白处平移</span>
        </div>
        <section class="board-wrap" id="board-wrap">
          <div id="board">
            <svg class="wires" id="wires"></svg>
            <div id="node-layer"></div>
            <div id="handle-layer"></div>
          </div>
        </section>
        <section class="stage-products" id="stage-products"></section>
      </div>
      <aside class="inspector" id="inspector"></aside>
    </div>`;
  renderPalette();
  renderBoard();
  renderInspector();
  renderStageProducts();
}

function renderTop() {
  const box = document.getElementById("top-actions");
  if (S.tab === "execution") {
    box.innerHTML = `<button type="button" class="btn" data-act="reset-execution-config" ${S.executionSaving ? "disabled" : ""}>恢复默认值</button><button type="button" class="btn primary" data-act="save-execution-config" ${S.executionSaving ? "disabled" : ""}>${S.executionSaving ? "保存中…" : "保存配置"}</button>`;
    return;
  }
  if (S.tab === "elements") {
    box.innerHTML = `
      <button type="button" class="btn primary" data-act="add-typed" data-type="${esc(S.elementTypeFilter)}">新增${esc(elementTypeLabel(S.elementTypeFilter))}</button>
      ${S.elementTypeFilter === "reference_image" ? '<button type="button" class="btn" data-act="open-reference-zip">ZIP 批量导入</button>' : ""}
      <button type="button" class="btn danger" data-act="delete-checked">删除勾选</button>
      <button type="button" class="btn" data-act="import">导入配置包</button>
      <button type="button" class="btn" data-act="export-library">备份全部配置</button>`;
    return;
  }
  if (S.tab === "runs") {
    box.innerHTML = '<button type="button" class="btn primary" data-act="add-run">新增记录</button><button type="button" class="btn" data-act="reload-runs">刷新</button>';
    return;
  }
  if (S.tab === "results") {
    box.innerHTML = '<button type="button" class="btn primary" data-act="add-result">新增作品</button><button type="button" class="btn" data-act="reload-results">刷新</button>';
    return;
  }
  if (S.tab !== "flow") {
    box.innerHTML = "";
    return;
  }
  if (!S.editing || !S.flow) {
    box.innerHTML = `
      <button type="button" class="btn primary" data-act="new-flow-meta">新建</button>
      <button type="button" class="btn" data-act="copy-flow" ${S.flows.length ? "" : "disabled"}>复制流程</button>
      <button type="button" class="btn danger" data-act="delete-checked-flows">删除勾选</button>
      <button type="button" class="btn" data-act="import">导入配置包</button>
      <button type="button" class="btn" data-act="export-flows">备份全部流程</button>
      <button type="button" class="btn primary" id="batch-run-open" data-act="open-batch-run" disabled>勾选批量执行</button>`;
    updateBatchRunButton();
    return;
  }
  box.innerHTML = `
    <button type="button" class="btn" data-act="back-flows">返回列表</button>
    <button type="button" class="btn" data-act="copy-flow" data-id="${esc(S.flow.id)}">复制流程</button>
    <button type="button" class="btn" data-act="export-flow">导出流程</button>
    <button type="button" class="btn" data-act="import">导入</button>
    <button type="button" class="btn danger" data-act="delete-flow">删除流程</button>
    <button type="button" class="btn primary" data-act="open-run" ${S.running ? "disabled" : ""}>循环执行</button>`;
}

function scheduleLabel(schedule) {
  if (!schedule?.enabled) return "未启用";
  const names = { daily: "每天", weekdays: "工作日", weekly: "每周", once: "只跑一次" };
  return (names[schedule.frequency] || "定时") + " " + (schedule.time || "");
}

function filteredFlows() {
  const query = (S.flowQuery || "").trim().toLowerCase();
  const rows = S.flows.filter((flow) => {
    if (S.flowGroupFilter && (flow.group || "") !== S.flowGroupFilter) return false;
    if (S.flowTagFilter && !(flow.tags || []).includes(S.flowTagFilter)) return false;
    if (S.flowStatusFilter && (flow.status || "启用") !== S.flowStatusFilter) return false;
    if (!query) return true;
    const fields = (flow.fields || []).flatMap((item) => [item.key, item.value]);
    const blob = [flow.id, flow.name, flow.description, flow.group, ...(flow.tags || []), ...fields].join(" ").toLowerCase();
    return blob.includes(query);
  });
  const direction = S.flowSort === "updated-asc" ? 1 : -1;
  rows.sort((a, b) => {
    if (S.flowSort === "name") return (a.name || "").localeCompare(b.name || "", "zh");
    const at = new Date(a.updatedAt || 0).getTime();
    const bt = new Date(b.updatedAt || 0).getTime();
    return (at - bt) * direction;
  });
  return rows;
}

function renderFlowToolbar() {
  const root = document.getElementById("flow-toolbar");
  if (!root) return;
  const groups = uniqueValues(S.flows.map((item) => item.group || ""));
  const tags = uniqueValues(S.flows.flatMap((item) => item.tags || []));
  root.innerHTML = `
    <input id="flow-search" placeholder="检索名称、说明、分组、标签、字段" value="${esc(S.flowQuery || "")}">
    <select id="flow-filter-group">
      <option value="">全部分组</option>
      ${groups.map((item) => `<option value="${esc(item)}" ${S.flowGroupFilter === item ? "selected" : ""}>${esc(item)}</option>`).join("")}
    </select>
    <select id="flow-filter-tag">
      <option value="">全部标签</option>
      ${tags.map((item) => `<option value="${esc(item)}" ${S.flowTagFilter === item ? "selected" : ""}>${esc(item)}</option>`).join("")}
    </select>
    <select id="flow-filter-status">
      <option value="" ${S.flowStatusFilter === "" ? "selected" : ""}>全部状态</option>
      <option value="启用" ${S.flowStatusFilter === "启用" ? "selected" : ""}>启用</option>
      <option value="停用" ${S.flowStatusFilter === "停用" ? "selected" : ""}>停用</option>
    </select>
    <button type="button" class="btn" data-act="flow-batch-status" data-status="启用">勾选标为启用</button>
    <button type="button" class="btn" data-act="flow-batch-status" data-status="停用">勾选标为停用</button>
    ${tableTransferControls("flows")}
    <select id="flow-sort">
      <option value="updated-desc" ${S.flowSort === "updated-desc" ? "selected" : ""}>更新时间新到旧</option>
      <option value="updated-asc" ${S.flowSort === "updated-asc" ? "selected" : ""}>更新时间旧到新</option>
      <option value="name" ${S.flowSort === "name" ? "selected" : ""}>名称</option>
    </select>
    ${selectionControls("flows")}`;
}

function renderFlowList() {
  const root = document.getElementById("flow-table");
  if (!root) return;
  const rows = filteredFlows();
  const checked = new Set(S.flowChecked || []);
  const visibleIds = rows.map((item) => item.id);
  const allChecked = visibleIds.length > 0 && visibleIds.every((id) => checked.has(id));
  const body = rows.map((flow) => {
    const nodes = flow.graph?.nodes?.length || 0;
    const edges = flow.graph?.edges?.length || 0;
    const tags = (flow.tags || []).map((tag) => `<span class="pill">${esc(tag)}</span>`).join(" ") || "—";
    const fields = (flow.fields || []).map((item) => `${item.key}=${item.value}`).join("，") || "—";
    const updated = flow.updatedAt ? new Date(flow.updatedAt).toLocaleString() : "—";
    return `<tr>
      <td><input type="checkbox" data-act="check-flow" data-id="${esc(flow.id)}" ${checked.has(flow.id) ? "checked" : ""}></td>
      <td title="${esc(flow.name || "未命名流程")}">${esc(flow.name || "未命名流程")}</td>
      <td title="${esc(flow.id)}">${esc(flow.id)}</td>
      <td>${esc(flow.group || "—")}</td>
      <td>${tags}</td>
      <td><span class="pill ${(flow.status || "启用") === "停用" ? "" : "green"}">${esc(flow.status || "启用")}</span></td>
      <td title="${esc(fields)}">${esc(fields)}</td>
      <td title="${esc(flow.description || "")}">${esc(flow.description || "—")}</td>
      <td>${nodes} 个节点 · ${edges} 条连线</td>
      <td>${esc(scheduleLabel(flow.schedule))}</td>
      <td>${esc(updated)}</td>
      <td class="row-actions">
        <button type="button" class="btn" data-act="open-flow" data-id="${esc(flow.id)}">进入</button>
        <button type="button" class="btn" data-act="edit-flow-meta" data-id="${esc(flow.id)}">编辑</button>
        <button type="button" class="btn" data-act="copy-flow" data-id="${esc(flow.id)}">复制流程</button>
        <button type="button" class="btn danger" data-act="delete-one-flow" data-id="${esc(flow.id)}">删除</button>
      </td>
    </tr>`;
  }).join("");
  root.innerHTML = `<table class="data-table"><thead><tr>
      <th><input type="checkbox" data-act="check-all-flows" ${allChecked ? "checked" : ""}></th>
      <th>名称</th><th>流程 ID</th><th>分组</th><th>标签</th><th>状态</th><th>字段</th><th>说明</th><th>结构</th><th>定时</th><th>更新时间</th><th>操作</th>
    </tr></thead><tbody>${body || '<tr><td colspan="12">没有符合条件的流程。</td></tr>'}</tbody></table>`;
  updateListSelection("flows", rows, root);
  updateBatchRunButton();
}

function renderExecutionConfig() {
  if (!S.executionDraft) S.executionDraft = clone(S.executionConfig);
  const fields = [
    ["flowConcurrency", "流程并发数", "最多同时运行多少个流程，批量任务、手动执行和定时任务共用这个上限。"],
    ["promptConcurrency", "提示词并发数", "单个流程内，同时生成多少条生图提示词。"],
    ["imageConcurrency", "任务内生图并发数", "单个流程内，同时向生图模型提交多少张图片任务。"],
  ];
  document.getElementById("main").innerHTML = `<div class="execution-settings"><section class="execution-intro"><span class="eyebrow">CONCURRENCY</span><h2>让多个流程一起运行</h2><p>三个层级分别设置。每个流程使用独立参考图、独立执行记录和独立输出文件夹。</p></section><div class="execution-grid">${fields.map(([key,label,description],index) => `<section class="execution-card"><span class="execution-step">0${index+1}</span><label class="field"><span>${label}</span><input type="number" id="execution-${key}" data-execution-key="${key}" aria-label="${label}" min="1" max="12" step="1" value="${esc(S.executionDraft[key])}" ${S.executionSaving ? "disabled" : ""}></label><p>${description}</p><small>可设置 1–12</small></section>`).join("")}</div><p id="execution-config-error" class="form-error" role="alert"></p><section class="execution-preview"><h3>当前配置预览</h3><p id="execution-config-preview" aria-live="polite"></p></section><p class="hint">保存后，流程并发数控制后续任务的启动；提示词和生图并发数用于新启动的流程。已发出的请求会继续完成。这里的设置统一管理执行并发，取代 API 配置中的旧并发参数。</p></div>`;
  updateExecutionPreview();
}

function updateExecutionPreview() {
  const config = S.executionDraft;
  const root = document.getElementById("execution-config-preview");
  if (!root || !config) return;
  const valid = Object.values(config).every((value) => Number.isInteger(value) && value >= 1 && value <= 12);
  root.textContent = valid ? `最多 ${config.flowConcurrency} 个流程同时运行；每个流程最多 ${config.promptConcurrency} 条提示词、${config.imageConcurrency} 张图片并发。任务充足时，整体最多 ${config.flowConcurrency * config.promptConcurrency} 个提示词请求或 ${config.flowConcurrency * config.imageConcurrency} 个生图请求同时进行。` : "请为三个参数填写1到12的整数。";
}

async function saveExecutionConfig() {
  if (S.executionSaving) return;
  const config = clone(S.executionDraft);
  const error = document.getElementById("execution-config-error");
  if (!Object.values(config).every((value) => Number.isInteger(value) && value >= 1 && value <= 12)) { error.textContent = "三个并发参数都必须为1到12的整数。"; return; }
  S.executionSaving = true; renderTop(); renderExecutionConfig();
  try {
    const result = await api("PUT", "/api/execution-config", config);
    S.executionConfig = result.config; S.executionDraft = clone(result.config);
    setBanner("执行配置已保存。", "ok");
  } catch (failure) { document.getElementById("execution-config-error").textContent = failure.message; }
  finally { S.executionSaving = false; renderTop(); if (S.tab === "execution") document.querySelectorAll('[data-execution-key]').forEach((el) => { el.disabled = false; }); }
}

function updateBatchRunButton() {
  const button = document.getElementById("batch-run-open");
  if (!button) return;
  const count = (S.flowChecked || []).filter((id) => S.flows.some((flow) => flow.id === id)).length;
  button.disabled = !count || S.running;
  button.textContent = count ? `勾选批量执行（${count}）` : "勾选批量执行";
}

function openBatchRun() {
  S.batchRunIds = (S.flowChecked || []).filter((id) => S.flows.some((flow) => flow.id === id));
  if (!S.batchRunIds.length) { setBanner("请先勾选要执行的流程。", "bad"); return; }
  const names = S.batchRunIds.map((id) => S.flows.find((flow) => flow.id === id).name);
  document.getElementById("batch-run-selection").innerHTML = `<strong>已勾选 ${names.length} 个流程</strong><div>${names.map((name) => `<span class="pill">${esc(name)}</span>`).join(" ")}</div>`;
  document.getElementById("batch-repeat").value = "1";
  document.querySelector('[name="batch-mode"][value="now"]').checked = true;
  document.getElementById("batch-frequency").value = "once";
  const date = new Date(Date.now() + 5 * 60 * 1000);
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString();
  document.getElementById("batch-date").value = local.slice(0, 10);
  document.getElementById("batch-time").value = local.slice(11, 16);
  document.querySelectorAll('#batch-weekdays input').forEach((el) => { el.checked = false; });
  document.getElementById("batch-continue").checked = false;
  document.getElementById("batch-run-error").textContent = "";
  updateBatchRunSummary();
  document.getElementById("batch-run-dialog").classList.remove("hidden");
  focusSoon("#batch-repeat");
}

function closeBatchRun() {
  if (S.batchRunBusy) return;
  document.getElementById("batch-run-dialog").classList.add("hidden");
  document.getElementById("batch-run-open")?.focus();
}

function updateBatchRunSummary() {
  const scheduled = document.querySelector('[name="batch-mode"]:checked')?.value === "schedule";
  const frequency = document.getElementById("batch-frequency").value;
  document.getElementById("batch-schedule-fields").classList.toggle("hidden", !scheduled);
  document.getElementById("batch-date-field").classList.toggle("hidden", frequency !== "once");
  document.getElementById("batch-weekdays").classList.toggle("hidden", frequency !== "weekly");
  const repeat = Number(document.getElementById("batch-repeat").value);
  const valid = Number.isInteger(repeat) && repeat >= 1 && repeat <= 50;
  const config = S.executionConfig;
  document.getElementById("batch-concurrency-hint").innerHTML = `当前配置：流程 ${config.flowConcurrency} 并发 · 每个流程提示词 ${config.promptConcurrency} 并发 · 生图 ${config.imageConcurrency} 并发。<button type="button" class="btn" data-act="open-execution-config">前往执行配置</button>`;
  document.getElementById("batch-run-summary").textContent = valid ? `${S.batchRunIds.length} 个流程 × ${repeat} 轮，每批共执行 ${S.batchRunIds.length * repeat} 次。每轮重新随机参考图，沿用各流程自己的作品命名与保存位置。` : "请输入1到50的整数循环次数。";
  document.querySelector('[data-act="submit-batch-run"]').textContent = scheduled ? "保存定时批次" : "开始批量执行";
}

async function submitBatchRun() {
  if (S.batchRunBusy) return;
  const error = document.getElementById("batch-run-error");
  error.textContent = "";
  const repeat = Number(document.getElementById("batch-repeat").value);
  if (!Number.isInteger(repeat) || repeat < 1 || repeat > 50) { error.textContent = "循环次数必须为1到50的整数。"; return; }
  const mode = document.querySelector('[name="batch-mode"]:checked').value;
  const body = { ids: [...S.batchRunIds], repeat, mode, continueOnError: document.getElementById("batch-continue").checked };
  if (mode === "schedule") {
    body.schedule = { frequency: document.getElementById("batch-frequency").value, date: document.getElementById("batch-date").value, time: document.getElementById("batch-time").value, weekdays: [...document.querySelectorAll('#batch-weekdays input:checked')].map((el) => Number(el.value)) };
    if (!body.schedule.time || (body.schedule.frequency === "once" && !body.schedule.date)) { error.textContent = "请设置定时日期和时间。"; return; }
    if (body.schedule.frequency === "once" && new Date(`${body.schedule.date}T${body.schedule.time}`) <= new Date()) { error.textContent = "请选择未来的执行时间。"; return; }
    if (body.schedule.frequency === "weekly" && !body.schedule.weekdays.length) { error.textContent = "请至少选择一个执行日。"; return; }
  }
  S.batchRunBusy = true;
  const dialog = document.getElementById("batch-run-dialog");
  dialog.querySelectorAll("button, input, select").forEach((el) => { el.disabled = true; });
  try {
    await api("POST", "/api/batch-runs", body);
    dialog.classList.add("hidden");
    await refreshBatchRuns();
    setBanner(mode === "schedule" ? "已保存定时批次。程序保持打开即可按时执行。" : "已加入批量执行队列，可在列表查看进度。", "ok");
  } catch (failure) { error.textContent = failure.message; }
  finally {
    S.batchRunBusy = false;
    dialog.querySelectorAll("button, input, select").forEach((el) => { el.disabled = false; });
  }
}

let batchRefreshBusy = false;
async function refreshBatchRuns() {
  if (batchRefreshBusy) return;
  batchRefreshBusy = true;
  try { const data = await api("GET", "/api/batch-runs"); S.batches = data.batches || []; renderBatchStatuses(); }
  finally { batchRefreshBusy = false; }
}

function renderBatchStatuses() {
  const root = document.getElementById("batch-status-list");
  if (!root) return;
  const labels = { queued: "等待执行", running: "执行中", scheduled: "已定时", completed: "已完成", failed: "已停止 · 执行失败", cancelled: "已停止", interrupted: "上次执行中断" };
  root.innerHTML = (S.batches || []).slice(0, 5).map((job) => {
    const active = ["queued", "running", "scheduled"].includes(job.status);
    const activeTasks = job.activeTasks || [];
    const when = job.status === "scheduled" ? `下次 ${new Date(job.nextRunAt).toLocaleString()}` : activeTasks.length ? `第 ${job.currentRound}/${job.repeat} 轮 · 同时执行 ${activeTasks.length} 个：${activeTasks.map((task) => task.name).join("、")}` : job.currentFlow ? `第 ${job.currentRound}/${job.repeat} 轮 · ${job.currentFlow}` : `${job.succeeded} 次成功${job.failed ? `，${job.failed} 次失败` : ""}`;
    return `<section class="batch-status"><div><strong>${esc(labels[job.status] || job.status)}</strong><span>${job.flowIds.length} 个流程 · ${job.repeat} 轮</span><small title="${esc(job.flowNames.join("、"))}">${esc(when)}</small>${job.lastError ? `<small class="form-error">${esc(job.lastError)}</small>` : ""}</div><div class="batch-progress"><span>${job.completed} / ${job.total}</span><progress max="${job.total}" value="${job.completed}" aria-label="批次执行进度"></progress></div>${active ? `<button type="button" class="btn" data-act="cancel-batch-run" data-id="${esc(job.id)}" ${job.stopRequested ? "disabled" : ""}>${job.stopRequested ? "等待在途流程完成" : job.status === "scheduled" ? "取消定时" : "停止后续执行"}</button>` : ""}</section>`;
  }).join("");
}

function openFlowMeta(flow) {
  S.flowDraft = clone(flow);
  S.flowDraft.group = S.flowDraft.group || "";
  S.flowDraft.tags = S.flowDraft.tags || [];
  S.flowDraft.fields = S.flowDraft.fields || [];
  document.getElementById("flow-dialog-title").textContent = flow.id ? "编辑流程资料" : "新建流程";
  const flowError = document.getElementById("flow-dialog-error");
  if (flowError) flowError.textContent = "";
  renderFlowDialog();
  document.getElementById("flow-dialog").classList.remove("hidden");
  focusSoon("#flow-dialog [data-bind='flow-meta.name']");
}

function renderFlowDialog() {
  const draft = S.flowDraft;
  const root = document.getElementById("flow-dialog-body");
  if (!draft || !root) return;
  const groups = uniqueValues(S.flows.map((item) => item.group || ""));
  root.innerHTML = `
    ${field("名称", `<input data-bind="flow-meta.name" value="${esc(draft.name || "")}">`)}
    ${field("说明", `<textarea data-bind="flow-meta.description">${esc(draft.description || "")}</textarea>`)}
    ${field("分组", `<input data-bind="flow-meta.group" list="flow-group-list" value="${esc(draft.group || "")}">`)}
    <datalist id="flow-group-list">${groups.map((item) => `<option value="${esc(item)}"></option>`).join("")}</datalist>
    ${field("标签，用逗号分开", `<input data-bind="flow-meta.tags" value="${esc((draft.tags || []).join("，"))}">`)}
    ${field("状态", `<select data-bind="flow-meta.status"><option value="启用" ${(draft.status || "启用") === "启用" ? "selected" : ""}>启用</option><option value="停用" ${draft.status === "停用" ? "selected" : ""}>停用</option></select>`)}
    <h3>字段</h3>
    ${(draft.fields || []).map((item, index) => `<div class="carry">
      <input data-bind="flow-field" data-index="${index}" data-key="key" value="${esc(item.key || "")}" placeholder="字段名">
      <input data-bind="flow-field" data-index="${index}" data-key="value" value="${esc(item.value || "")}" placeholder="字段值">
      <button type="button" data-act="delete-flow-field" data-index="${index}">删</button>
    </div>`).join("")}
    <div class="row"><button type="button" class="btn" data-act="add-flow-field">添加字段</button></div>`;
}

function closeFlowMeta() {
  S.flowDraft = null;
  document.getElementById("flow-dialog").classList.add("hidden");
}

function openFlowCopy(id) {
  const selected = (S.flowChecked || []).filter((flowId) => S.flows.some((flow) => flow.id === flowId));
  S.flowCopyIds = id ? [id] : selected.length ? selected : [S.flow?.id || S.flows[0]?.id].filter(Boolean);
  if (!S.flowCopyIds.length) { setBanner("先新建或选择一条流程。", "bad"); return; }
  const select = document.getElementById("flow-copy-source");
  select.innerHTML = (S.flowCopyIds.length > 1 ? `<option value="" data-checked="1">已勾选的 ${S.flowCopyIds.length} 条流程</option>` : "") + S.flows.map((flow) => `<option value="${esc(flow.id)}">${esc(flow.name)}（${esc(flow.id)}）</option>`).join("");
  select.value = S.flowCopyIds.length > 1 ? "" : S.flowCopyIds[0];
  document.getElementById("flow-copy-count").value = "1";
  document.getElementById("flow-copy-schedule").checked = false;
  document.getElementById("flow-copy-error").textContent = "";
  updateFlowCopySummary();
  document.getElementById("flow-copy-dialog").classList.remove("hidden");
  focusSoon("#flow-copy-count");
}

function flowCopySources() {
  const select = document.getElementById("flow-copy-source");
  return select.selectedOptions[0]?.dataset.checked === "1" ? S.flowCopyIds : [select.value].filter(Boolean);
}

function updateFlowCopySummary() {
  const sourceCount = flowCopySources().length;
  const count = Number(document.getElementById("flow-copy-count").value);
  document.getElementById("flow-copy-summary").textContent = Number.isInteger(count) && count > 0 ? `每条复制 ${count} 份，共生成 ${sourceCount * count} 条新流程。` : "请输入正整数份数。";
}

async function copyFlows() {
  if (S.flowCopyBusy) return;
  const ids = flowCopySources();
  const count = Number(document.getElementById("flow-copy-count").value);
  const error = document.getElementById("flow-copy-error");
  error.textContent = "";
  if (!ids.length || !Number.isInteger(count) || count < 1 || ids.length * count > 1000) {
    error.textContent = "复制份数必须为正整数，每次最多生成 1000 条流程。";
    return;
  }
  const keepSchedule = document.getElementById("flow-copy-schedule").checked;
  const dialog = document.getElementById("flow-copy-dialog");
  const submit = dialog.querySelector('[data-act="submit-flow-copy"]');
  S.flowCopyBusy = true;
  dialog.querySelectorAll("button, input, select").forEach((input) => { input.disabled = true; });
  submit.textContent = "正在复制…";
  try {
    if (S.editing) await persistNow();
    const data = await api("POST", "/api/flows/copy", { ids, count, keepSchedule });
    S.flows = [...data.flows, ...S.flows];
    dialog.classList.add("hidden");
    S.flowCopyIds = [];
    if (S.editing) { renderChrome(); renderTop(); }
    else { renderFlowToolbar(); renderFlowList(); }
    setBanner(`已复制 ${data.count} 条流程，名称和 ID 已自动追加编号。`, "ok");
  } catch (failure) {
    error.textContent = failure.message;
  } finally {
    S.flowCopyBusy = false;
    dialog.querySelectorAll("button, input, select").forEach((input) => { input.disabled = false; });
    submit.textContent = "开始复制";
  }
}

function renderPalette() {
  const groups = [];
  for (const item of S.nodeTypes) {
    let group = groups.find((entry) => entry.name === item.group);
    if (!group) {
      group = { name: item.group, items: [] };
      groups.push(group);
    }
    group.items.push(item);
  }
  document.getElementById("palette").innerHTML = '<p class="nav-label">组件</p>' + groups.map((group) => `
    <h3>${esc(group.name)}</h3>
    ${group.items.map((item) => `<button type="button" class="pal" data-act="add-node" data-type="${esc(item.type)}" title="${esc(item.blurb)}">${esc(item.label)}</button>`).join("")}
  `).join("") + '<p class="hint foot">把节点拖到画布上，或点一下就加在最右边。从右侧圆点拖到下一个节点的左侧圆点，就能连上；线两端的圆点可以再拖去改指向。</p>';
}

function renderBoard() {
  const layer = document.getElementById("node-layer");
  if (!layer || !S.flow) return;
  layer.innerHTML = S.flow.graph.nodes.map(nodeCard).join("");
  applyZoom();
  drawWires();
}

function applyZoom() {
  const board = document.getElementById("board");
  if (board) board.style.zoom = String(S.zoom || 1);
  const label = document.getElementById("zoom-label");
  if (label) label.textContent = Math.round((S.zoom || 1) * 100) + "%";
}

function setZoom(next) {
  S.zoom = Math.max(0.5, Math.min(1.6, Math.round(next * 10) / 10));
  applyZoom();
}

function nodeCard(node) {
  const info = typeInfo(node.type);
  const selected = S.selected?.kind === "node" && S.selected.id === node.id;
  const trace = S.lastTrace?.nodes?.[node.id];
  const product = (info.product || []).join("、");
  return `
    <article class="node ${selected ? "on" : ""} ${S.linking === node.id ? "linking" : ""}" data-drag="1" data-type="${esc(node.type)}" data-id="${esc(node.id)}" data-act="select-node" style="left:${node.x || 0}px;top:${node.y || 0}px">
      <header data-drag="1">
        <span class="kind">${trace ? `<i class="dot ${esc(trace.status)}"></i>` : ""}${esc(info.label)}</span>
        <strong class="node-name">${esc(node.name || info.label)}</strong>
      </header>
      <button type="button" class="port in" data-act="port-in" data-id="${esc(node.id)}" title="入口，把上一段拖到这里">入</button>
      <button type="button" class="port out" data-act="port-out" data-id="${esc(node.id)}" title="出口，按住拖到下一个节点">出</button>
      <p class="node-meta">导出 ${esc(product || "—")} · 元素 ${(node.imports || []).length}</p>
    </article>`;
}

function drawWires() {
  const svg = document.getElementById("wires");
  const board = document.getElementById("board");
  if (!svg || !board || !S.flow) return;
  const size = boardSize();
  board.style.width = size.width + "px";
  board.style.height = size.height + "px";
  svg.setAttribute("width", size.width);
  svg.setAttribute("height", size.height);
  const nodes = Object.fromEntries(S.flow.graph.nodes.map((node) => [node.id, node]));
  const parts = [];
  const handles = [];
  for (const edge of S.flow.graph.edges) {
    const from = nodes[edge.from];
    const to = nodes[edge.to];
    if (!from || !to) continue;
    const x1 = (from.x || 0) + 220;
    const y1 = (from.y || 0) + 54;
    const x2 = to.x || 0;
    const y2 = (to.y || 0) + 54;
    const curve = Math.max(24, Math.min(90, Math.abs(x2 - x1) / 2));
    const d = `M ${x1} ${y1} C ${x1 + curve} ${y1}, ${x2 - curve} ${y2}, ${x2} ${y2}`;
    const on = S.selected?.kind === "edge" && S.selected.id === edge.id ? "on" : "";
    const label = (edge.carry || []).map((item) => item.target).filter(Boolean).join(" + ") || "未携带";
    parts.push(`<path class="wire ${on}" d="${d}"></path><path class="hit" d="${d}" data-act="select-edge" data-id="${esc(edge.id)}"></path><text class="wire-label" x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 8}">${esc(label)}</text>`);
    handles.push(`<button type="button" class="wire-handle" data-edge-end="from" data-id="${esc(edge.id)}" style="left:${x1 + 18}px;top:${y1 - 6}px" title="拖到别的出口"></button>`);
    handles.push(`<button type="button" class="wire-handle" data-edge-end="to" data-id="${esc(edge.id)}" style="left:${x2 - 30}px;top:${y2 - 6}px" title="拖到别的入口"></button>`);
  }
  if (S.wireDrag) {
    const from = nodes[S.wireDrag.fromId];
    const anchor = S.wireDrag.end === "from" && from
      ? { x: (from.x || 0), y: (from.y || 0) + 54 }
      : from
        ? { x: (from.x || 0) + 220, y: (from.y || 0) + 54 }
        : null;
    if (anchor) {
      const x2 = S.wireDrag.x;
      const y2 = S.wireDrag.y;
      const curve = Math.max(24, Math.min(90, Math.abs(x2 - anchor.x) / 2));
      parts.push(`<path class="wire draft" d="M ${anchor.x} ${anchor.y} C ${anchor.x + curve} ${anchor.y}, ${x2 - curve} ${y2}, ${x2} ${y2}"></path>`);
    }
  }
  svg.innerHTML = parts.join("");
  const handleLayer = document.getElementById("handle-layer");
  if (handleLayer) handleLayer.innerHTML = handles.join("");
}

function renderInspector() {
  const root = document.getElementById("inspector");
  if (!root) return;
  if (!S.flow) {
    root.innerHTML = "";
    return;
  }
  if (S.selected?.kind === "node") {
    const node = findNode(S.selected.id);
    root.innerHTML = node ? nodeInspector(node) : "";
    return;
  }
  if (S.selected?.kind === "edge") {
    const edge = findEdge(S.selected.id);
    root.innerHTML = edge ? edgeInspector(edge) : "";
    return;
  }
  root.innerHTML = flowInspector();
}

function flowInspector() {
  const schedule = S.flow.schedule || {};
  const weekly = schedule.frequency === "weekly";
  const once = schedule.frequency === "once";
  const days = ["日", "一", "二", "三", "四", "五", "六"];
  return `
    <h2>${esc(S.flow.name)}</h2>
    <p class="hint">${S.flow.graph.nodes.length} 个节点，${S.flow.graph.edges.length} 条连线。点空白处回到这里。</p>
    ${field("流程名称", `<input data-bind="flow.name" value="${esc(S.flow.name)}">`)}
    ${field("说明", `<textarea data-bind="flow.description" rows="4">${esc(S.flow.description || "")}</textarea>`)}
    <h3>定时</h3>
    <label class="check"><input type="checkbox" data-bind="schedule.enabled" ${schedule.enabled ? "checked" : ""}> 启用定时</label>
    ${field("频率", `<select data-bind="schedule.frequency">
      <option value="daily" ${schedule.frequency === "daily" ? "selected" : ""}>每天</option>
      <option value="weekdays" ${schedule.frequency === "weekdays" ? "selected" : ""}>工作日</option>
      <option value="weekly" ${schedule.frequency === "weekly" ? "selected" : ""}>每周</option>
      <option value="once" ${schedule.frequency === "once" ? "selected" : ""}>只跑一次</option>
    </select>`)}
    ${field("时间", `<input type="time" data-bind="schedule.time" value="${esc(schedule.time || "09:00")}">`)}
    ${weekly ? `<div class="days">${days.map((label, index) => `<label class="check"><input type="checkbox" data-bind="schedule.weekday" data-day="${index}" ${(schedule.weekdays || []).includes(index) ? "checked" : ""}>${label}</label>`).join("")}</div>` : ""}
    ${once ? field("日期", `<input type="date" data-bind="schedule.date" value="${esc(schedule.date || "")}">`) : ""}
    <p class="hint" id="next-run">${esc(nextLabel(schedule))}</p>
    <p class="hint">程序开着才会到点执行。错过之后，下次打开会补跑一次。</p>`;
}

function nodeInspector(node) {
  const info = typeInfo(node.type);
  const params = node.params || (node.params = {});
  const hints = [...(info.hints || []), ...COMMON_HINTS];
  const trace = S.lastTrace?.nodes?.[node.id];
  let extra = "";
  if (node.type === "start.theme") {
    extra += field("本次固定主题，留空则用导入的主题元素", `<input data-bind="node.themeText" value="${esc(params.themeText || "")}">`);
  }
  if (node.type === "prompt.compose") {
    extra += `<label class="check"><input type="checkbox" data-bind="node.useModel" ${params.useModel ? "checked" : ""}> 调用模型生成提示词</label>
      <p class="hint">没配文本模型时，仍然只把字段填进模板。</p>`;
  }
  if (["prompt.from_refs", "prompt.from_multi_refs"].includes(node.type)) {
    if (node.type === "prompt.from_multi_refs") {
      extra += field("随机参考图数量 N（1 到 12）", `<input type="number" min="1" max="12" step="1" data-bind="node.referenceCount" value="${esc(params.referenceCount ?? 2)}">`);
    }
    extra += field("生成条数（1 到 12）", `<input type="number" min="1" max="12" data-bind="node.count" value="${esc(params.count || 3)}">`);
    extra += field("提示词用途", `<select data-bind="node.purpose">
      <option value="image" ${(params.purpose || "image") === "image" ? "selected" : ""}>生图</option>
      <option value="title" ${params.purpose === "title" ? "selected" : ""}>标题</option>
      <option value="article" ${params.purpose === "article" ? "selected" : ""}>文章</option>
    </select>`);
    extra += node.type === "prompt.from_multi_refs"
      ? '<p class="hint">在当前分组、标签筛选结果中勾选候选图，每轮不重复随机抽取 N 张，一起发给生文模型。本轮所有提示词共用这组图片；参考图数量与生成条数分别设置。不足 N 张会提示。成功后每张入选图的使用次数加 1。</p>'
      : '<p class="hint">先按分组、标签筛参考图，再勾选多张。执行时从勾选里随机带 1 张给生文模型。这一步成功后，该图的使用次数加 1。</p>';
  }
  if (node.type === "text.title" || node.type === "text.article") {
    extra += field("材料", `<select data-bind="node.contextMode">
      <option value="auto" ${(params.contextMode || "auto") === "auto" ? "selected" : ""}>自动附带参考材料</option>
      <option value="template-only" ${params.contextMode === "template-only" ? "selected" : ""}>只用提示词模板</option>
    </select>`);
    extra += '<p class="hint">入口里如果已经有 prompt.text，会用那一份，盖过下面勾选的提示词。</p>';
  }
  if (node.type === "image.generate") {
    extra += imageSizeSettings(node);
    extra += '<p class="hint">入口的 prompts 有几条就生几张，无需生图配置组。使用配置组时保留其张数和风格，整批图片尺寸统一按本节点设置执行。</p>';
  }
  if (node.type === "file.output") {
    extra += field("保存方式", `<select data-bind="node.saveAs">
      <option value="" ${!params.saveAs ? "selected" : ""}>沿用输出目标</option>
      <option value="zip" ${params.saveAs === "zip" ? "selected" : ""}>ZIP</option>
      <option value="library" ${params.saveAs === "library" ? "selected" : ""}>结果库</option>
      <option value="both" ${params.saveAs === "both" ? "selected" : ""}>ZIP 和结果库</option>
    </select>`);
    extra += field("保存路径", `<input data-bind="node.directory" value="${esc(params.directory || "")}" spellcheck="false" placeholder="D:\\壁纸\\可爱 或 outputs/cute-wallpaper">`);
    extra += '<div class="row"><button type="button" class="btn" data-act="pick-output-dir" data-target="node">选择文件夹</button></div>';
    extra += '<p class="hint">结果库里的每张图用 UUID 命名，分组和标签等于本次随机带上的那张参考图。</p>';
  }
  if (node.type === "work.generate") {
    extra += field("作品名称模板（文件夹和 ZIP）", `<input data-bind="node.nameTemplate" value="${esc(params.nameTemplate || '${title}')}" spellcheck="false" placeholder="\${title}">`);
    extra += field("保存路径", `<input data-bind="node.directory" value="${esc(params.directory || 'outputs/works')}" spellcheck="false" placeholder="outputs/works 或 D:\\作品">`);
    extra += '<div class="row"><button type="button" class="btn" data-act="pick-output-dir" data-target="node">选择文件夹</button></div>';
    extra += '<p class="hint">将入口 images 中的全部图片保存到作品文件夹，并生成同名 ZIP。名称支持 ${title}、{{title}} 和其他携带参数；重名时自动追加编号。</p>';
  }
  if (node.type === "result.direct") {
    const groups = [...new Set((S.results || []).map((item) => item.group || "").filter(Boolean))];
    extra += field("结果库分组", `<select data-bind="node.resultGroup"><option value="">请选择</option>${groups.map((item) => `<option value="${esc(item)}" ${params.group === item ? "selected" : ""}>${esc(item)}</option>`).join("")}</select>`);
    extra += field("张数 N", `<input type="number" min="1" max="50" data-bind="node.count" value="${esc(params.count || 1)}">`);
    extra += field("ZIP 保存路径", `<input data-bind="node.directory" value="${esc(params.directory || "")}" spellcheck="false" placeholder="outputs/result-direct 或 D:\\壁纸\\直出">`);
    extra += '<div class="row"><button type="button" class="btn" data-act="pick-output-dir" data-target="node">选择文件夹</button></div>';
    extra += field("ZIP 名称", `<input data-bind="node.zipName" value="${esc(params.zipName || "结果直出-{{date}}")}" spellcheck="false">`);
    extra += '<p class="hint">从该分组里随机抽 N 张「未出作品」的图，打成 ZIP。循环执行时每次另存一份，不覆盖上一次。成功后这些图会标成已出作品。</p>';
  }
  const keep = node.type !== "start.theme"
    ? `<label class="check"><input type="checkbox" data-bind="node.keepInlet" ${params.keepInlet ? "checked" : ""}> 把入口字段留在出口</label>
       <p class="hint">普通字段按连线选择传递；下方设置的携带参数会沿连线继续传递。</p>`
    : "";
  return `
    <h2>节点配置</h2>
    <p class="hint">${esc(info.label)} · ${esc(info.blurb || "")}</p>
    ${field("名称", `<input data-bind="node.name" value="${esc(node.name || "")}">`)}
    ${extra}
    ${keep}
    ${parameterBindings(node)}
    <h3>导入的元素</h3>
    ${importChecks(node, info)}
    <h3>执行前规则</h3>
    ${ruleChecks(node, "rulesBefore")}
    <h3>执行后规则</h3>
    ${ruleChecks(node, "rulesAfter")}
    <h3>出口字段</h3>
    <p class="hint">${esc(hints.join("、"))}</p>
    ${traceBlock(trace)}
    <div class="row">
      <button type="button" data-act="duplicate-node">复制节点</button>
      <button type="button" data-act="delete-selected">删除节点</button>
    </div>`;
}

function imageSizePreset(node) {
  return node.params?.sizePreset || ((S.flow?.name || '').includes('头像') ? 'avatar' : 'wallpaper');
}

function imageSizeSettings(node) {
  const preset = imageSizePreset(node);
  const params = node.params || {};
  const options = S.imageSizePresets.length ? S.imageSizePresets : [
    { id: 'avatar', label: '头像默认 · 1024 × 1024 · 1:1', width: 1024, height: 1024 },
    { id: 'wallpaper', label: '壁纸默认 · 1080 × 1920 · 9:16', width: 1080, height: 1920 },
  ];
  const selected = options.find(item => item.id === preset);
  return `<section class="image-size-settings"><h3>整批统一尺寸</h3>
    ${field('尺寸预设', `<select data-bind="node.sizePreset">${options.map(item => `<option value="${esc(item.id)}" ${preset === item.id ? 'selected' : ''}>${esc(item.label)}</option>`).join('')}<option value="custom" ${preset === 'custom' ? 'selected' : ''}>自定义像素尺寸</option></select>`)}
    ${preset === 'custom' ? `<div class="image-size-fields">
      ${field('宽度（像素）', `<input type="number" min="256" max="4096" step="1" data-bind="node.imageWidth" value="${esc(params.imageWidth ?? 1080)}">`)}
      ${field('高度（像素）', `<input type="number" min="256" max="4096" step="1" data-bind="node.imageHeight" value="${esc(params.imageHeight ?? 1920)}">`)}
    </div>` : `<p class="hint">本批目标：${selected ? `${selected.width} × ${selected.height} 像素` : '请选择有效尺寸'}。</p>`}
    <p class="hint">同一节点所有图片统一使用此尺寸，覆盖提示词和配置组中的尺寸。接口返回尺寸不符时等比缩放并用模糊背景补边，保留完整主体，最终保存为指定尺寸的 PNG。</p>
  </section>`;
}

const PARAMETER_SOURCES = [
  ["referenceUsed.name", "参考图名称"], ["referenceUsed.note", "参考图说明"],
  ["referenceNames", "多张参考图名称（合并）"], ["referenceNotes", "多张参考图说明（合并）"],
  ["title", "上游 title"], ["theme.text", "主题"],
  ["titles.items.0.text", "首个生成标题"], ["prompt.text", "提示词文本"],
];

function parameterBindings(node) {
  const bindings = node.params?.carryParams || [];
  const multi = node.type === "prompt.from_multi_refs";
  return `<section class="parameter-bindings"><h3>携带参数</h3>
    <p class="hint">来源字段或固定文案 AS 参数名。参数会沿连线一直传到后续节点，可在作品名称中使用 \${title}。</p>
    ${multi ? '<p class="hint">多图可选择「多张参考图说明（合并）」AS title，用所有入选图的说明命名作品；「参考图名称 / 说明」取本轮首张图。结果库分组和标签沿用首张图。</p>' : ''}
    ${bindings.map((item, index) => {
      const literal = item.sourceType === "literal";
      const custom = !literal && !PARAMETER_SOURCES.some(([source]) => source === item.source);
      return `<div class="parameter-row">
        <label><span class="hint">来源</span><select data-bind="parameter.source" data-index="${index}">
          ${PARAMETER_SOURCES.map(([source, label]) => `<option value="${source}" ${!literal && source === item.source ? "selected" : ""}>${multi && source.startsWith('referenceUsed.') ? '首张' : ''}${label}</option>`).join("")}
          <option value="__literal__" ${literal ? "selected" : ""}>固定文案</option>
          <option value="__custom__" ${custom ? "selected" : ""}>自定义字段</option>
        </select></label><span class="parameter-as">AS</span>
        <label><span class="hint">参数名</span><input data-bind="parameter.target" data-index="${index}" value="${esc(item.target || '')}" placeholder="\${title}" spellcheck="false"></label>
        <button type="button" class="btn" data-act="delete-parameter" data-index="${index}" aria-label="删除携带参数">删</button>
        ${literal ? `<label class="parameter-custom"><span class="hint">固定文案（直接填写，无需引号）</span><input data-bind="parameter.value" data-index="${index}" value="${esc(item.value ?? '')}" placeholder="例如 暗黑系壁纸分享" spellcheck="false"></label>` : ""}
        ${custom ? `<input class="parameter-custom" data-bind="parameter.customSource" data-index="${index}" value="${esc(item.source || '')}" placeholder="例如 referenceUsed.name" spellcheck="false">` : ""}
      </div>`;
    }).join("")}
    <button type="button" class="btn" data-act="add-parameter">添加携带参数</button>
  </section>`;
}

function importChecks(node, info) {
  const specs = info.elementTypes || [];
  if (!specs.length) return '<p class="hint">这个节点不导入元素。</p>';
  return specs.map((spec) => {
    let list = S.elements.filter((item) => item.type === spec.type && (item.status || "启用") !== "停用");
    if (spec.kind) list = list.filter((item) => item.payload?.kind === spec.kind);
    let filters = "";
    if (spec.type === "reference_image") {
      const params = node.params || {};
      const groups = [...new Set(list.map((item) => item.group || "").filter(Boolean))];
      const tags = [...new Set(list.flatMap((item) => item.tags || []))];
      if (params.refGroup) list = list.filter((item) => (item.group || "") === params.refGroup);
      if (params.refTag) list = list.filter((item) => (item.tags || []).includes(params.refTag));
      const picked = (node.imports || []).filter((entry) => entry.elementType === "reference_image").length;
      filters = `<div class="row">
        <select data-bind="node.refGroup"><option value="">全部分组</option>${groups.map((item) => `<option value="${esc(item)}" ${params.refGroup === item ? "selected" : ""}>${esc(item)}</option>`).join("")}</select>
        <select data-bind="node.refTag"><option value="">全部标签</option>${tags.map((item) => `<option value="${esc(item)}" ${params.refTag === item ? "selected" : ""}>${esc(item)}</option>`).join("")}</select>
      </div><div class="row">
        <button type="button" class="btn" data-act="select-node-references" ${list.length ? "" : "disabled"}>全选筛选结果</button>
        <button type="button" class="btn" data-act="clear-node-references" ${list.length ? "" : "disabled"}>取消勾选筛选结果</button>
      </div><p class="hint" data-reference-count>${esc(referenceSelectionSummary(node, list.length, picked, list.filter((item) => (node.imports || []).some((entry) => entry.elementId === item.id)).length))}</p>`;
    }
    const boxes = list.map((item) => {
      const checked = (node.imports || []).some((entry) => entry.elementId === item.id);
      const detail = item.payload?.purpose || item.payload?.kind || item.payload?.role || "";
      const used = item.type === "reference_image" ? ` <span class="muted">已用 ${Number(item.useCount || item.payload?.useCount || 0)} 次</span>` : "";
      const meta = [item.group, ...(item.tags || [])].filter(Boolean).join(" / ");
      return `<label class="check"><input type="checkbox" data-bind="import" data-element-id="${esc(item.id)}" data-element-type="${esc(item.type)}" ${checked ? "checked" : ""}>${esc(item.name)}${detail ? ` <span class="muted">${esc(detail)}</span>` : ""}${used}${meta ? `<br><span class="muted">${esc(meta)}</span>` : ""}</label>`;
    }).join("");
    return `<p class="hint">${esc(spec.label)}</p>${filters}${boxes || '<p class="hint">这一组里还没有参考图。</p>'}`;
  }).join("");
}

function selectNodeReferences(checked) {
  const node = S.selected?.kind === "node" ? findNode(S.selected.id) : null;
  if (!node) return;
  const inputs = [...document.querySelectorAll('#inspector [data-bind="import"][data-element-type="reference_image"]')];
  const visibleIds = new Set(inputs.map((input) => input.dataset.elementId));
  node.imports = (node.imports || []).filter((item) => !visibleIds.has(item.elementId));
  if (checked) inputs.forEach((input) => {
    node.imports.push({ alias: "reference_image", elementType: "reference_image", elementId: input.dataset.elementId });
  });
  inputs.forEach((input) => { input.checked = checked; });
  updateNodeImportSummary(node);
  touch();
}

function updateNodeImportSummary(node) {
  const meta = document.querySelector(`.node[data-id="${CSS.escape(node.id)}"] .node-meta`);
  if (meta) meta.textContent = `导出 ${(typeInfo(node.type).product || []).join("、") || "—"} · 元素 ${(node.imports || []).length}`;
  const count = document.querySelector("#inspector [data-reference-count]");
  if (count) {
    const inputs = [...document.querySelectorAll('#inspector [data-bind="import"][data-element-type="reference_image"]')];
    const visible = inputs.length;
    const picked = (node.imports || []).filter((item) => item.elementType === "reference_image").length;
    count.textContent = referenceSelectionSummary(node, visible, picked, inputs.filter((input) => input.checked).length);
  }
}

function referenceSelectionSummary(node, visible, picked, checkedVisible) {
  if (node.type === "prompt.from_multi_refs") {
    return `当前筛选 ${visible} 张，其中已勾选 ${checkedVisible} 张；总共勾选 ${picked} 张。执行时仅从当前筛选内已勾选且启用的图片中随机抽取 ${node.params?.referenceCount ?? 2} 张。全选和取消勾选仅作用于当前筛选结果。`;
  }
  return `当前筛选 ${visible} 张，已勾选 ${picked} 张。全选和取消勾选仅作用于当前筛选结果；执行时从全部勾选中随机带 1 张。`;
}

function ruleChecks(node, bucket) {
  const rules = S.elements.filter((item) => item.type === "rule");
  if (!rules.length) return '<p class="hint">还没有规则。到元素页新建。</p>';
  return rules.map((item) => {
    const checked = (node[bucket] || []).includes(item.id);
    return `<label class="check"><input type="checkbox" data-bind="${bucket}" data-rule-id="${esc(item.id)}" ${checked ? "checked" : ""}>${esc(item.name)}</label>`;
  }).join("");
}

function mediaUrl(filePath) {
  const normalized = String(filePath || "").replace(/\\/g, "/");
  if (/^(work|outputs|uploads|results)\//.test(normalized)) {
    return "/media/" + normalized.split("/").map((part) => encodeURIComponent(part)).join("/");
  }
  const marker = "/data/";
  const index = normalized.toLowerCase().lastIndexOf(marker);
  if (index < 0) return "";
  const relative = normalized.slice(index + marker.length);
  if (!/^(work|outputs|uploads|results)\//.test(relative)) return "";
  return "/media/" + relative.split("/").map((part) => encodeURIComponent(part)).join("/");
}

function promptLines(value) {
  if (Array.isArray(value)) return value.map((item) => (typeof item === "string" ? item : item?.text || "")).filter(Boolean);
  if (value && Array.isArray(value.items)) return value.items.map((item) => item?.text || "").filter(Boolean);
  return [];
}

function productBits(exportData) {
  const data = exportData || {};
  const parts = [];
  const prompts = promptLines(data.prompts);
  if (prompts.length) {
    parts.push(`<ol class="product-list">${prompts.map((text) => `<li>${esc(text)}</li>`).join("")}</ol>`);
  }
  const titles = data.titles?.items || [];
  if (titles.length) {
    parts.push(`<ol class="product-list">${titles.map((item) => `<li>${esc(item.text || "")}</li>`).join("")}</ol>`);
  }
  const articles = data.articles?.items || [];
  if (articles.length) {
    parts.push(articles.map((item) => `<p><b>${esc(item.title || "文章")}</b></p><p class="hint">${esc((item.body || "").slice(0, 240))}</p>`).join(""));
  }
  const references = data.referencesUsed || data.images?.referencesUsed;
  const used = data.referenceUsed || data.images?.referenceUsed;
  if (references?.length) {
    parts.push(`<p class="hint">本次共同参考 ${references.length} 张图片</p><ol class="product-list">${references.map((item) => `<li>${esc(item.name || '')} · ${esc(item.group || '未分组')} · 使用 ${Number(item.useCount || 0)} 次${item.note ? `<br><span class="hint">${esc(item.note)}</span>` : ''}</li>`).join('')}</ol>`);
  } else if (used) {
    const tags = (used.tags || []).join("、");
    parts.push(`<p class="hint">本次参考图：${esc(used.name || "")} · 分组 ${esc(used.group || "未分组")} · 标签 ${esc(tags || "无")} · 使用 ${esc(String(used.useCount || ""))} 次</p>`);
  }
  const images = data.images?.items || [];
  if (images.length) {
    const size = data.images?.size || data.imageSize;
    if (size) parts.push(`<p class="hint">统一尺寸：${esc(size.width)} × ${esc(size.height)} 像素 · ${esc(size.ratio)}</p>`);
    parts.push(`<div class="product-images">${images.map((item) => {
      const src = mediaUrl(item.path);
      const label = esc(item.path || "");
      return src
        ? `<a href="${src}" target="_blank" rel="noreferrer"><img src="${src}" alt=""></a>`
        : `<span class="hint">${label}</span>`;
    }).join("")}</div>`);
  }
  const output = data.output || {};
  if (output.folder || output.zip) {
    parts.push(`<div class="row">
      ${output.folder ? `<button type="button" data-act="open-path" data-path="${encodeURIComponent(output.folder)}">打开文件夹</button><span class="hint">${esc(output.folder)}</span>` : ""}
      ${output.zip ? `<button type="button" data-act="open-path" data-path="${encodeURIComponent(output.zip)}">打开 ZIP</button><span class="hint">${esc(output.zip)}</span>` : ""}
    </div>`);
  }
  return parts.join("");
}

function traceBlock(trace) {
  if (!trace) return '<p class="hint">还没有这一步的产物。执行一次之后会出现在这里。</p>';
  return `<div class="trace ${esc(trace.status)}"><strong>${trace.status === "failed" ? "上次失败" : "上次产物"}</strong><div>${esc(trace.message || "")}</div>${productBits(trace.export)}</div>`;
}

function renderStageProducts() {
  const root = document.getElementById("stage-products");
  if (!root) return;
  const trace = S.lastTrace;
  if (!trace?.nodes) {
    root.innerHTML = "<h3>阶段产物</h3><p class=\"hint\">执行之后，每一步的提示词、图片和保存位置会显示在这里。</p>";
    return;
  }
  const order = trace.order || Object.keys(trace.nodes);
  const cards = order.map((id) => {
    const node = trace.nodes[id] || {};
    const ok = node.status === "succeeded";
    return `<section class="product-card">
      <header><b>${esc(node.name || id)}</b><span class="pill ${ok ? "green" : "red"}">${ok ? "完成" : esc(node.status || "未执行")}</span></header>
      <p class="hint">${esc(node.message || "")}</p>
      ${productBits(node.export)}
    </section>`;
  }).join("");
  root.innerHTML = `<h3>阶段产物</h3>${cards}`;
}

function outputDirectory() {
  const node = (S.flow?.graph.nodes || []).find((item) => item.type === "work.generate") || (S.flow?.graph.nodes || []).find((item) => item.type === "file.output");
  if (node?.params?.directory) return node.params.directory;
  const imported = (node?.imports || []).find((item) => item.elementType === "output_target");
  const element = S.elements.find((item) => item.id === imported?.elementId);
  return element?.payload?.directory || "";
}

function edgeInspector(edge) {
  const from = findNode(edge.from);
  const to = findNode(edge.to);
  const hints = [...new Set([...(typeInfo(from?.type).hints || []), ...(typeInfo(to?.type).hints || []), ...COMMON_HINTS])];
  const rules = S.elements.filter((item) => item.type === "rule");
  return `
    <h2>连线配置</h2>
    <p class="hint">${esc(from?.name || edge.from)} → ${esc(to?.name || edge.to)}</p>
    <div class="row"><button type="button" data-act="edit-connection">选择携带字段</button></div>
    <datalist id="field-hints">${hints.map((hint) => `<option value="${esc(hint)}"></option>`).join("")}</datalist>
    ${(edge.carry || []).map((item, index) => `
      <div class="carry">
        <input data-bind="carry.source" data-index="${index}" list="field-hints" value="${esc(item.source || "")}" placeholder="来源字段">
        <select data-bind="carry.op" data-index="${index}">
          <option value="copy" ${item.op !== "template" ? "selected" : ""}>原样</option>
          <option value="template" ${item.op === "template" ? "selected" : ""}>模板</option>
        </select>
        <input data-bind="carry.target" data-index="${index}" list="field-hints" value="${esc(item.target || "")}" placeholder="写入字段">
        <button type="button" data-act="delete-carry" data-index="${index}">删</button>
        ${item.op === "template" ? `<input data-bind="carry.template" data-index="${index}" value="${esc(item.template || "")}" placeholder="{{theme.text}}" style="grid-column:1 / -1">` : ""}
      </div>`).join("")}
    <button type="button" data-act="add-carry">添加字段</button>
    <h3>这条连线上的规则</h3>
    ${rules.map((item) => `<label class="check"><input type="checkbox" data-bind="edge.rules" data-rule-id="${esc(item.id)}" ${(edge.rules || []).includes(item.id) ? "checked" : ""}>${esc(item.name)}</label>`).join("") || '<p class="hint">还没有规则。</p>'}
    <div class="row"><button type="button" data-act="delete-selected">删除连线</button></div>
    <p class="hint">来源和写入字段支持 title 或 \${title}。节点设置的携带参数会自动沿连线传递。原样复制时，来源字段不存在就跳过；规则在复制之后执行。</p>`;
}

function field(label, html) {
  return `<label class="field"><span>${label}</span>${html}</label>`;
}

const TABLE_COLUMNS = {
  domain: [["name", "名称"], ["summary", "简介"], ["audience", "读者"], ["tone", "语气"], ["tags", "标签"]],
  theme: [["name", "名称"], ["text", "主题"], ["notes", "备注"]],
  reference_image: [["name", "名称"], ["role", "角色"], ["path", "路径"], ["note", "说明"]],
  reference_article: [["name", "名称"], ["title", "标题"], ["usage", "用途"], ["body", "正文"]],
  reference_copy: [["name", "名称"], ["usage", "用途"], ["text", "文案"]],
  prompt: [["name", "名称"], ["purpose", "用途"], ["notes", "备注"], ["template", "模板"]],
  image_config_group: [["name", "名称"], ["variants", "配置"]],
  model_profile: [["name", "名称"], ["kind", "种类"], ["model", "模型名"], ["baseUrl", "接口地址"]],
  output_target: [["name", "名称"], ["directory", "目录"], ["writeFolder", "文件夹"], ["writeZip", "ZIP"]],
  rule: [["name", "名称"], ["steps", "步骤"]],
};

function libraryType() {
  if (S.libraryType && S.elementTypes.some((item) => item.type === S.libraryType)) return S.libraryType;
  return currentElement()?.type || S.elementTypes[0]?.type || "prompt";
}

function cellText(element, key) {
  if (key === "name") return element.name || "";
  const payload = element.payload || {};
  if (key === "variants") return (payload.variants || []).map((item) => item.name || "未命名").join("、") || "—";
  if (key === "steps") return (payload.steps || []).length + " 步";
  if (key === "writeFolder" || key === "writeZip") return payload[key] ? "是" : "否";
  const value = payload[key];
  if (Array.isArray(value)) return value.join("，");
  if (value === null || value === undefined || value === "") return "—";
  return String(value).replace(/\s+/g, " ");
}

function uniqueValues(items) {
  return [...new Set(items.filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh"));
}

function filteredElements() {
  const query = (S.elementQuery || "").trim().toLowerCase();
  return S.elements.filter((element) => {
    if (element.type !== S.elementTypeFilter) return false;
    if (S.elementGroupFilter && (element.group || "") !== S.elementGroupFilter) return false;
    if (S.elementTagFilter && !(element.tags || []).includes(S.elementTagFilter)) return false;
    if (S.elementStatusFilter && (element.status || "启用") !== S.elementStatusFilter) return false;
    if (!query) return true;
    const fields = (element.fields || []).flatMap((item) => [item.key, item.value]);
    const blob = [element.id, element.name, element.group, elementTypeLabel(element.type), ...(element.tags || []), ...fields, JSON.stringify(element.payload || {})].join(" ").toLowerCase();
    return blob.includes(query);
  }).sort((a, b) => (a.group || "").localeCompare(b.group || "", "zh") || elementTypeLabel(a.type).localeCompare(elementTypeLabel(b.type), "zh") || (a.name || "").localeCompare(b.name || "", "zh"));
}

function renderElementToolbar() {
  const root = document.getElementById("element-toolbar");
  if (!root) return;
  const scoped = S.elements.filter((item) => item.type === S.elementTypeFilter);
  const groups = uniqueValues(scoped.map((item) => item.group || ""));
  const tags = uniqueValues(scoped.flatMap((item) => item.tags || []));
  root.innerHTML = `
    <input id="element-search" aria-label="检索元素" placeholder="检索名称、内容、路径、标签、字段" value="${esc(S.elementQuery || "")}">
    <select id="element-filter-group">
      <option value="">全部分组</option>
      ${groups.map((item) => `<option value="${esc(item)}" ${S.elementGroupFilter === item ? "selected" : ""}>${esc(item)}</option>`).join("")}
    </select>
    <select id="element-filter-tag">
      <option value="">全部标签</option>
      ${tags.map((item) => `<option value="${esc(item)}" ${S.elementTagFilter === item ? "selected" : ""}>${esc(item)}</option>`).join("")}
    </select>
    <select id="element-filter-status">
      <option value="" ${S.elementStatusFilter === "" ? "selected" : ""}>全部状态</option>
      <option value="启用" ${S.elementStatusFilter === "启用" ? "selected" : ""}>启用</option>
      <option value="停用" ${S.elementStatusFilter === "停用" ? "selected" : ""}>停用</option>
    </select>
    <button type="button" class="btn" data-act="element-batch-status" data-status="启用">勾选标为启用</button>
    <button type="button" class="btn" data-act="element-batch-status" data-status="停用">勾选标为停用</button>
    ${tableTransferControls("elements")}
    ${selectionControls("elements")}`;
}

function renderElementTable() {
  const root = document.getElementById("element-table");
  if (!root) return;
  const rows = filteredElements();
  const checked = new Set(S.elementChecked || []);
  const visibleIds = rows.map((item) => item.id);
  const allChecked = visibleIds.length > 0 && visibleIds.every((id) => checked.has(id));
  const contentColumns = (TABLE_COLUMNS[S.elementTypeFilter] || []).filter(([key]) => key !== "name");
  const body = rows.map((element) => {
    const tags = (element.tags || []).map((tag) => `<span class="pill">${esc(tag)}</span>`).join(" ") || "—";
    const fields = (element.fields || []).map((item) => `${item.key}=${item.value}`).join("，") || "—";
    return `<tr data-act="edit-element" data-id="${esc(element.id)}">
      <td><input type="checkbox" data-act="check-element" data-id="${esc(element.id)}" ${checked.has(element.id) ? "checked" : ""}></td>
      <td>${esc(element.name || "未命名")}${element.type === "reference_image" ? ` <span class="hint">已用 ${Number(element.useCount || element.payload?.useCount || 0)} 次</span>` : ""}</td>
      ${contentColumns.map(([key]) => `<td title="${esc(cellText(element, key))}">${key === "path" && mediaUrl(element.payload?.path) ? `<img class="list-thumb" loading="lazy" src="${mediaUrl(element.payload.path)}" alt="参考图预览">` : ""}${esc(cellText(element, key))}</td>`).join("")}
      <td>${esc(element.group || "—")}</td>
      <td>${tags}</td>
      <td><span class="pill ${(element.status || "启用") === "停用" ? "" : "green"}">${esc(element.status || "启用")}</span></td>
      <td title="${esc(fields)}">${esc(fields)}</td>
      <td>${esc(element.updatedAt ? new Date(element.updatedAt).toLocaleString() : "—")}</td>
      <td class="row-actions"><button type="button" class="btn" data-act="edit-element" data-id="${esc(element.id)}">编辑</button><button type="button" class="btn danger" data-act="delete-one" data-id="${esc(element.id)}">删除</button></td>
    </tr>`;
  }).join("");
  root.innerHTML = `<table class="data-table"><thead><tr>
      <th><input type="checkbox" data-act="check-all-elements" ${allChecked ? "checked" : ""}></th>
      <th>名称</th>${contentColumns.map(([, label]) => `<th>${esc(label)}</th>`).join("")}<th>分组</th><th>标签</th><th>状态</th><th>字段</th><th>更新时间</th><th>操作</th>
    </tr></thead><tbody>${body || `<tr><td colspan="${8 + contentColumns.length}">没有符合条件的元素，可新增或导入表格。</td></tr>`}</tbody></table>`;
  updateListSelection("elements", rows, root);
}

function openElementEditor(element) {
  S.elementDraft = clone(element);
  S.elementDraft.group = S.elementDraft.group || "";
  S.elementDraft.tags = S.elementDraft.tags || [];
  S.elementDraft.fields = S.elementDraft.fields || [];
  S.elementDraft.payload = S.elementDraft.payload || clone(S.elementDefaults[S.elementDraft.type] || {});
  if (S.elementDraft.type === "reference_image") {
    const aliases = { style: "风格", subject: "主体", composition: "构图", color: "色彩", colour: "色彩" };
    const role = S.elementDraft.payload.role || "风格";
    S.elementDraft.payload.role = aliases[role] || role;
  }
  document.getElementById("element-dialog-title").textContent = elementFormTitle(S.elementDraft);
  document.getElementById("element-dialog-id").textContent = element.id ? "编号 " + element.id : "保存后写入元素库";
  const elementError = document.getElementById("element-dialog-error");
  if (elementError) elementError.textContent = "";
  renderElementDialog();
  document.getElementById("element-dialog").classList.remove("hidden");
  focusSoon("#element-dialog [data-bind='element.name']");
}

function renderElementDialog() {
  const draft = S.elementDraft;
  const root = document.getElementById("element-dialog-body");
  if (!draft || !root) return;
  const groups = uniqueValues(S.elements.map((item) => item.group || ""));
  const typeField = field("类型", `<input value="${esc(elementTypeLabel(draft.type))}" disabled>`);
  root.innerHTML = `
    ${field("名称", `<input data-bind="element.name" value="${esc(draft.name || "")}" autocomplete="off">`)}
    ${typeField}
    ${field("分组", `<input data-bind="element.group" list="element-group-list" value="${esc(draft.group || "")}" placeholder="例如 壁纸案例">`)}
    <datalist id="element-group-list">${groups.map((item) => `<option value="${esc(item)}"></option>`).join("")}</datalist>
    ${field("标签，用逗号分开", `<input data-bind="element.tags" value="${esc((draft.tags || []).join("，"))}">`)}
    ${field("状态", `<select data-bind="element.status"><option value="启用" ${(draft.status || "启用") === "启用" ? "selected" : ""}>启用</option><option value="停用" ${draft.status === "停用" ? "selected" : ""}>停用</option></select>`)}
    <h3>字段</h3>
    ${(draft.fields || []).map((item, index) => `<div class="carry">
      <input data-bind="custom-field" data-index="${index}" data-key="key" value="${esc(item.key || "")}" placeholder="字段名">
      <input data-bind="custom-field" data-index="${index}" data-key="value" value="${esc(item.value || "")}" placeholder="字段值">
      <button type="button" data-act="delete-custom-field" data-index="${index}">删</button>
    </div>`).join("")}
    <div class="row"><button type="button" class="btn" data-act="add-custom-field">添加字段</button></div>
    <h3>类型内容</h3>
    ${elementFields(draft)}`;
}

function closeElementEditor() {
  S.elementDraft = null;
  document.getElementById("element-dialog").classList.add("hidden");
}

function openReferenceZip() {
  const dialog = document.getElementById("reference-zip-dialog");
  dialog.querySelectorAll("input, textarea").forEach((input) => { input.value = ""; });
  document.getElementById("reference-zip-group").value = S.elementGroupFilter || "";
  document.getElementById("reference-zip-tags").value = S.elementTagFilter || "";
  document.getElementById("reference-zip-groups").innerHTML = uniqueValues(S.elements.filter((item) => item.type === "reference_image").map((item) => item.group)).map((group) => `<option value="${esc(group)}"></option>`).join("");
  document.getElementById("reference-zip-error").textContent = "";
  dialog.classList.remove("hidden");
}

async function importReferenceZip() {
  if (S.referenceZipBusy) return;
  const error = document.getElementById("reference-zip-error");
  error.textContent = "";
  const file = document.getElementById("reference-zip-file").files?.[0];
  if (!file || !/\.zip$/i.test(file.name)) { error.textContent = "请选择 ZIP 压缩包。"; return; }
  if (file.size > 200_000_000) { error.textContent = "ZIP 不能超过 200MB。"; return; }
  const group = document.getElementById("reference-zip-group").value.trim();
  const tags = resultTags(document.getElementById("reference-zip-tags").value);
  const note = document.getElementById("reference-zip-note").value;
  const query = new URLSearchParams({ name: file.name, group, tags: JSON.stringify(tags), note });
  const dialog = document.getElementById("reference-zip-dialog");
  const submit = dialog.querySelector('[data-act="submit-reference-zip"]');
  S.referenceZipBusy = true;
  dialog.querySelectorAll("button, input, textarea").forEach((input) => { input.disabled = true; });
  submit.textContent = "正在导入…";
  let imported = null;
  try {
    const response = await fetch("/api/reference-images/import-zip?" + query, { method: "POST", body: file });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "ZIP 导入失败");
    imported = data;
    dialog.classList.add("hidden");
    S.elementQuery = "";
    S.elementGroupFilter = group;
    S.elementTagFilter = "";
    S.elementStatusFilter = "";
    S.elementChecked = [];
    await load();
    setBanner(`已导入 ${data.created} 张参考图${data.skipped ? `，跳过 ${data.skipped} 个非图片文件` : ""}。`, "ok");
  } catch (failure) {
    if (imported) setBanner(`已导入 ${imported.created} 张参考图，列表刷新失败，请重新打开参考图列表。`, "bad");
    else error.textContent = failure.message;
  } finally {
    S.referenceZipBusy = false;
    dialog.querySelectorAll("button, input, textarea").forEach((input) => { input.disabled = false; });
    submit.textContent = "开始导入";
  }
}

function elementFields(element) {
  const payload = element.payload || (element.payload = {});
  if (element.type === "domain") {
    return [
      field("简介", `<textarea data-bind="payload.summary">${esc(payload.summary || "")}</textarea>`),
      field("读者", `<input data-bind="payload.audience" value="${esc(payload.audience || "")}">`),
      field("语气", `<input data-bind="payload.tone" value="${esc(payload.tone || "")}">`),
      field("标签，用逗号分开", `<input data-bind="payload.tags" value="${esc((payload.tags || []).join("，"))}">`),
    ].join("");
  }
  if (element.type === "theme") {
    return [
      field("主题", `<input data-bind="payload.text" value="${esc(payload.text || "")}">`),
      field("备注", `<textarea data-bind="payload.notes">${esc(payload.notes || "")}</textarea>`),
    ].join("");
  }
  if (element.type === "reference_article") {
    return [
      field("标题", `<input data-bind="payload.title" value="${esc(payload.title || "")}">`),
      field("用途", selectHtml("payload.usage", payload.usage, ["结构", "语气", "事实", "综合"])),
      field("正文", `<textarea data-bind="payload.body" rows="10">${esc(payload.body || "")}</textarea>`),
    ].join("");
  }
  if (element.type === "reference_copy") {
    return [
      field("用途", selectHtml("payload.usage", payload.usage, ["标题", "开头", "结尾", "金句"])),
      field("文案", `<textarea data-bind="payload.text">${esc(payload.text || "")}</textarea>`),
    ].join("");
  }
  if (element.type === "reference_image") {
    const path = payload.path || "";
    const roles = ["风格", "主体", "构图", "色彩"];
    if (payload.role && !roles.includes(payload.role)) roles.unshift(payload.role);
    const preview = path ? `<img class="preview" alt="" src="/media/${esc(encodeURI(path))}">` : "";
    return [
      `<p class="hint">已使用 ${Number(element.useCount || payload.useCount || 0)} 次。分组和标签在下方，节点里可以按它们筛选。</p>`,
      field("角色", selectHtml("payload.role", payload.role, roles)),
      field("路径", `<input data-bind="payload.path" value="${esc(path)}" spellcheck="false" placeholder="选图片后自动填上，也可以手写">`),
      `<div class="row"><input type="file" id="ref-file" accept="image/*"><button type="button" class="btn" data-act="upload-image">上传并预览</button><span class="hint">选好图片就会写入路径。点保存才进入元素库。</span></div>`,
      preview,
      field("说明", `<input data-bind="payload.note" value="${esc(payload.note || "")}">`),
    ].join("");
  }
  if (element.type === "prompt") {
    return [
      field("用途", selectHtml("payload.purpose", payload.purpose, [["title", "标题"], ["article", "文章"], ["image", "生图"], ["system", "系统"], ["other", "其他"]])),
      field("模板，用 {{theme.text}} 这类字段", `<textarea data-bind="payload.template" rows="12" spellcheck="false">${esc(payload.template || "")}</textarea>`),
      field("备注", `<textarea data-bind="payload.notes">${esc(payload.notes || "")}</textarea>`),
    ].join("");
  }
  if (element.type === "image_config_group") {
    const variants = payload.variants || (payload.variants = []);
    return variants.map((variant, index) => `
      <h3>配置 ${index + 1}</h3>
      ${field("名称", `<input data-bind="variant" data-index="${index}" data-key="name" value="${esc(variant.name || "")}">`)}
      ${field("宽", `<input type="number" data-bind="variant" data-index="${index}" data-key="width" value="${esc(variant.width || 1080)}">`)}
      ${field("高", `<input type="number" data-bind="variant" data-index="${index}" data-key="height" value="${esc(variant.height || 1920)}">`)}
      ${field("张数，没有上游多条提示词时使用，最多 12 张", `<input type="number" data-bind="variant" data-index="${index}" data-key="count" value="${esc(variant.count || 1)}">`)}
      ${field("风格", `<input data-bind="variant" data-index="${index}" data-key="style" value="${esc(variant.style || "")}">`)}
      ${field("不要出现", `<input data-bind="variant" data-index="${index}" data-key="negative" value="${esc(variant.negative || "")}">`)}
      <button type="button" data-act="delete-variant" data-index="${index}">删除这组</button>
    `).join("") + '<div class="row"><button type="button" data-act="add-variant">添加一组配置</button></div>';
  }
  if (element.type === "model_profile") {
    const status = S.keyStatus[element.id];
    const keyLine = status?.count ? `已保存 ${status.count} 条密钥` : "还没有密钥";
    return [
      field("种类", selectHtml("payload.kind", payload.kind || "text", [["text", "文本"], ["image", "生图"]])),
      field("接口地址", `<input data-bind="payload.baseUrl" value="${esc(payload.baseUrl || "")}" spellcheck="false">`),
      field("模型名", `<input data-bind="payload.model" value="${esc(payload.model || "")}" spellcheck="false">`),
      field("温度", `<input type="number" step="0.1" data-bind="payload.temperature" value="${esc(payload.temperature ?? 0.7)}">`),
      field("最大输出，可留空", `<input type="number" data-bind="payload.maxTokens" value="${esc(payload.maxTokens ?? "")}">`),
      `<p class="hint">提示词和生图并发统一在左侧「执行配置」设置。</p>`,
      `<p class="hint">${esc(keyLine)}。同一节点有多条生文或生图时按执行配置中的并发数执行，每条任务轮流用一把密钥。余额不足、密钥无效或无权限时自动换下一条重试，本次流程后续任务跳过已失效密钥。密钥不进导出文件。</p>`,
      (status?.keys || []).map((item, index) => `<div class="row"><span class="hint">密钥 ${index + 1} ····${esc(item.last4 || "")}</span><button type="button" data-act="remove-key" data-index="${index}">删除</button></div>`).join(""),
      field("追加密钥，一行一条", `<textarea id="api-keys" rows="4" autocomplete="off" spellcheck="false" placeholder="sk-...&#10;sk-...">${esc(element._apiKeys || "")}</textarea>`),
      `<div class="row"><button type="button" data-act="clear-key">清除全部密钥</button>${payload.kind !== "image" ? '<button type="button" data-act="test-model">测试文本接口</button>' : ""}</div>`,
    ].join("");
  }
  if (element.type === "output_target") {
    return [
      field("保存路径，可写绝对路径", `<input data-bind="payload.directory" value="${esc(payload.directory || "")}" spellcheck="false" placeholder="D:\\壁纸\\可爱 或 outputs/cute-wallpaper">`),
      `<label class="check"><input type="checkbox" data-bind="payload.writeFolder" ${payload.writeFolder !== false ? "checked" : ""}> 写文件夹</label>`,
      `<label class="check"><input type="checkbox" data-bind="payload.writeZip" ${payload.writeZip ? "checked" : ""}> 打包 ZIP</label>`,
      field("ZIP 名称，可用 {{theme.text}} 和 {{date}}", `<input data-bind="payload.zipName" value="${esc(payload.zipName || "")}">`),
    ].join("");
  }
  if (element.type === "rule") {
    const steps = payload.steps || (payload.steps = []);
    return steps.map((step, index) => `
      <h3>步骤 ${index + 1}</h3>
      ${field("动作", selectHtml("step", step.op, [["require", "必须有值"], ["set", "写入字段"], ["template", "按模板写入"], ["drop", "丢掉字段"], ["append_note", "追加备注"], ["block", "中止流程"]], index))}
      ${field("字段", `<input data-bind="step" data-index="${index}" data-key="field" value="${esc(step.field || "")}" placeholder="titles.items">`)}
      ${field("来源字段", `<input data-bind="step" data-index="${index}" data-key="from" value="${esc(step.from || "")}" placeholder="titles.items.0.text">`)}
      ${field("模板", `<input data-bind="step" data-index="${index}" data-key="template" value="${esc(step.template || "")}" placeholder="{{theme.text}}">`)}
      ${field("固定值", `<input data-bind="step" data-index="${index}" data-key="value" value="${esc(step.value || "")}">`)}
      ${field("中止时的说明", `<input data-bind="step" data-index="${index}" data-key="message" value="${esc(step.message || "")}">`)}
      <button type="button" data-act="delete-step" data-index="${index}">删除步骤</button>
    `).join("") + '<div class="row"><button type="button" data-act="add-step">添加步骤</button></div>';
  }
  return "";
}

function selectHtml(bind, current, options, index) {
  const extra = bind === "step" ? ` data-index="${index}" data-key="op"` : "";
  const html = options.map((option) => {
    const value = Array.isArray(option) ? option[0] : option;
    const label = Array.isArray(option) ? option[1] : option;
    return `<option value="${esc(value)}" ${current === value ? "selected" : ""}>${esc(label)}</option>`;
  }).join("");
  return `<select data-bind="${bind}"${extra}>${html}</select>`;
}

function filteredRuns() {
  const query = (S.runQuery || "").trim().toLowerCase();
  return (S.runs || []).filter((run) => {
    if (S.runStatusFilter && run.status !== S.runStatusFilter) return false;
    if (S.runFlowFilter && run.flowId !== S.runFlowFilter) return false;
    if (S.runModeFilter && run.mode !== S.runModeFilter) return false;
    if (!query) return true;
    const blob = [run.id, run.flowId, S.flows.find((flow) => flow.id === run.flowId)?.name, run.error, run.mode, JSON.stringify(run.trace || {})].join(" ").toLowerCase();
    return blob.includes(query);
  });
}

function renderRuns() {
  const root = document.getElementById("runs");
  if (!root) return;
  const restoreFocus = restoreListFocus(root);
  const rows = filteredRuns();
  const checked = new Set(S.runChecked || []);
  const visibleIds = rows.map((item) => item.id);
  const allChecked = visibleIds.length > 0 && visibleIds.every((id) => checked.has(id));
  const body = rows.map((run) => {
    const ok = run.status === "succeeded";
    const folder = run.trace?.output?.folder || "";
    const zip = run.trace?.output?.zip || "";
    return `<tr>
      <td><input type="checkbox" data-act="check-run" data-id="${esc(run.id)}" ${checked.has(run.id) ? "checked" : ""}></td>
      <td title="${esc(run.id)}">${esc(S.flows.find((flow) => flow.id === run.flowId)?.name || run.flowId || "未关联流程")}</td>
      <td>${esc(run.startedAt ? new Date(run.startedAt).toLocaleString() : "—")}</td>
      <td>${run.mode === "schedule" ? "定时" : "单次"}</td>
      <td><span class="pill ${ok ? "green" : "red"}">${ok ? "已完成" : "失败"}</span></td>
      <td title="${esc(run.error || "")}">${esc(run.error || "—")}</td>
      <td class="row-actions">
        <button type="button" class="btn" data-act="edit-run" data-id="${esc(run.id)}">编辑</button>
        <button type="button" class="btn danger" data-act="delete-run" data-id="${esc(run.id)}">删除</button>
        ${folder ? `<button type="button" class="btn" data-act="open-path" data-path="${encodeURIComponent(folder)}">文件夹</button>` : ""}
        ${zip ? `<button type="button" class="btn" data-act="open-path" data-path="${encodeURIComponent(zip)}">ZIP</button>` : ""}
      </td>
    </tr>`;
  }).join("");
  root.innerHTML = `
    <div class="list-toolbar">
      <input id="run-search" placeholder="检索编号或错误" value="${esc(S.runQuery || "")}">
      <select id="run-filter-flow" aria-label="筛选流程"><option value="">全部流程</option>${uniqueValues(S.runs.map((item) => item.flowId)).map((id) => `<option value="${esc(id)}" ${S.runFlowFilter === id ? "selected" : ""}>${esc(S.flows.find((flow) => flow.id === id)?.name || id)}</option>`).join("")}</select>
      <select id="run-filter-mode" aria-label="筛选执行方式"><option value="">全部方式</option><option value="once" ${S.runModeFilter === "once" ? "selected" : ""}>单次</option><option value="schedule" ${S.runModeFilter === "schedule" ? "selected" : ""}>定时</option></select>
      <select id="run-filter-status">
        <option value="" ${S.runStatusFilter === "" ? "selected" : ""}>全部状态</option>
        <option value="succeeded" ${S.runStatusFilter === "succeeded" ? "selected" : ""}>已完成</option>
        <option value="failed" ${S.runStatusFilter === "failed" ? "selected" : ""}>失败</option>
      </select>
      <button type="button" class="btn danger" data-act="delete-checked-runs">删除勾选</button>
      <button type="button" class="btn" data-act="run-batch-status" data-status="succeeded">勾选标为已完成</button>
      <button type="button" class="btn" data-act="run-batch-status" data-status="failed">勾选标为失败</button>
      ${tableTransferControls("runs")}
      ${selectionControls("runs")}
    </div>
    <div class="table-panel"><table class="data-table"><thead><tr>
      <th><input type="checkbox" data-act="check-all-runs" ${allChecked ? "checked" : ""}></th>
      <th>流程</th><th>时间</th><th>方式</th><th>状态</th><th>错误</th><th>操作</th>
    </tr></thead><tbody>${body || '<tr><td colspan="7">没有符合条件的记录，可新增或导入表格。</td></tr>'}</tbody></table></div>`;
  updateListSelection("runs", rows, root);
  restoreFocus();
}

function openRunEditor(run = {}) {
  S.runDraft = clone(run);
  document.getElementById("run-record-title").textContent = run.id ? "编辑运行记录" : "新增运行记录";
  document.getElementById("run-record-body").innerHTML = `
    ${field("流程", `<select data-run="flowId"><option value="">未关联流程</option>${S.flows.map((flow) => `<option value="${esc(flow.id)}" ${flow.id === run.flowId ? "selected" : ""}>${esc(flow.name)}</option>`).join("")}${run.flowId && !S.flows.some((flow) => flow.id === run.flowId) ? `<option value="${esc(run.flowId)}" selected>${esc(run.flowId)}</option>` : ""}</select>`)}
    ${field("执行方式", `<select data-run="mode"><option value="once">单次</option><option value="schedule" ${run.mode === "schedule" ? "selected" : ""}>定时</option></select>`)}
    ${field("状态", `<select data-run="status"><option value="succeeded">已完成</option><option value="failed" ${run.status === "failed" ? "selected" : ""}>失败</option></select>`)}
    ${field("开始时间", `<input data-run="startedAt" value="${esc(run.startedAt || new Date().toISOString())}">`)}
    ${field("错误说明", `<textarea data-run="error">${esc(run.error || "")}</textarea>`)}
    <p class="hint">这里维护运行记录，保存不会执行流程。</p>`;
  document.getElementById("run-record-error").textContent = "";
  document.getElementById("run-record-dialog").classList.remove("hidden");
}

async function saveRunEditor() {
  if (!S.runDraft) return;
  const body = { ...S.runDraft };
  document.querySelectorAll("[data-run]").forEach((input) => { body[input.dataset.run] = input.value; });
  if (!body.startedAt || Number.isNaN(Date.parse(body.startedAt))) throw new Error("请输入有效的开始时间");
  await api(body.id ? "PUT" : "POST", body.id ? "/api/runs/" + encodeURIComponent(body.id) : "/api/runs", body);
  document.getElementById("run-record-dialog").classList.add("hidden");
  S.runDraft = null;
  await loadRuns();
  renderRuns();
  setBanner("运行记录已保存。", "ok");
}

async function saveResultDialog() {
  const draft = S.resultDraft;
  if (!draft) return;
  const root = document.getElementById("result-dialog-body");
  const group = root.querySelector("[data-bind='result.group']")?.value || "";
  const tags = resultTags(root.querySelector("[data-bind='result.tags']")?.value || "");
  const workedValue = root.querySelector("[data-bind='result.worked']")?.value;
  const error = document.getElementById("result-dialog-error");
  try {
    if (draft.mode === "add") {
      const files = [...(document.getElementById("result-files")?.files || [])];
      if (!files.length) {
        if (error) error.textContent = "请选择图片。";
        return;
      }
      for (const file of files) {
        const response = await fetch("/api/uploads?name=" + encodeURIComponent(file.name), { method: "POST", body: file });
        const uploaded = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(uploaded.error || "上传失败");
        await api("POST", "/api/results", {
          sourcePath: uploaded.path,
          name: file.name.replace(/\.[^.]+$/, ""),
          group,
          tags,
          worked: workedValue === "1",
        });
      }
    } else if (draft.mode === "batch") {
      const body = { action: "update", ids: draft.ids };
      if (group.trim()) body.group = group.trim();
      if (tags.length) body.tags = tags;
      if (workedValue === "0" || workedValue === "1") body.worked = workedValue === "1";
      await api("POST", "/api/results/batch", body);
    } else {
      await api("PUT", "/api/results/" + encodeURIComponent(draft.id), {
        name: root.querySelector("[data-bind='result.name']")?.value || draft.name,
        group,
        tags,
        prompt: root.querySelector("[data-bind='result.prompt']")?.value || "",
        worked: workedValue === "1",
      });
    }
    closeResultDialog();
    await loadResults();
    renderResults();
    setBanner("结果库已更新。", "ok");
  } catch (error) {
    if (document.getElementById("result-dialog-error")) document.getElementById("result-dialog-error").textContent = error.message;
    setBanner(error.message, "bad");
  }
}

async function deleteResults(ids) {
  const list = (ids || []).filter(Boolean);
  if (!list.length) { setBanner("先勾选结果图。", "bad"); return; }
  if (!await confirmAction(`删除 ${list.length} 张结果图？`)) return;
  await api("POST", "/api/results/batch", { action: "delete", ids: list });
  S.resultChecked = (S.resultChecked || []).filter((id) => !list.includes(id));
  await loadResults();
  renderResults();
  setBanner("已删除。", "ok");
}

async function setResultStatus(ids, worked) {
  const list = (ids || []).filter(Boolean);
  if (!list.length) { setBanner("先勾选结果图。", "bad"); return; }
  await api("POST", "/api/results/batch", { action: "status", ids: list, worked });
  await loadResults();
  renderResults();
  setBanner(worked ? "已标为已出作品。" : "已标为未出作品。", "ok");
}

async function loadResults() {
  const data = await api("GET", "/api/results?limit=0");
  S.results = data.results || [];
}

function filteredResults() {
  const query = (S.resultQuery || "").trim().toLowerCase();
  return (S.results || []).filter((item) => {
    if (S.resultGroupFilter && (item.group || "") !== S.resultGroupFilter) return false;
    if (S.resultTagFilter && !(item.tags || []).includes(S.resultTagFilter)) return false;
    if (S.resultStatusFilter === "unused" && item.worked) return false;
    if (S.resultStatusFilter === "worked" && !item.worked) return false;
    if (!query) return true;
    const blob = [item.id, item.name, item.group, item.prompt, item.path, item.flowName, item.referenceName, ...(item.tags || [])].join(" ").toLowerCase();
    return blob.includes(query);
  });
}

function renderResults() {
  const root = document.getElementById("results");
  if (!root) return;
  const restoreFocus = restoreListFocus(root);
  const groups = [...new Set((S.results || []).map((item) => item.group || "").filter(Boolean))];
  const rows = filteredResults();
  const checked = new Set(S.resultChecked || []);
  const visibleIds = rows.map((item) => item.id);
  const allChecked = visibleIds.length > 0 && visibleIds.every((id) => checked.has(id));
  const body = rows.map((item) => {
    const src = mediaUrl(item.path);
    const tags = (item.tags || []).map((tag) => `<span class="pill">${esc(tag)}</span>`).join(" ") || "—";
    return `<tr>
      <td><input type="checkbox" data-act="check-result" data-id="${esc(item.id)}" ${checked.has(item.id) ? "checked" : ""}></td>
      <td>${src ? `<a href="${src}" target="_blank" rel="noreferrer"><img class="list-thumb" loading="lazy" src="${src}" alt="结果图预览"></a>` : "—"}</td>
      <td title="${esc(item.id)}">${esc(item.name || "结果图")}</td>
      <td>${esc(item.group || "—")}</td>
      <td>${tags}</td>
      <td><span class="pill ${item.worked ? "" : "green"}">${item.worked ? "已出作品" : "未出作品"}</span></td>
      <td title="${esc(item.prompt || "")}">${esc((item.prompt || "").slice(0, 48) || "—")}</td>
      <td>${esc(item.referenceName || "—")}</td>
      <td>${esc(item.createdAt ? new Date(item.createdAt).toLocaleString() : "—")}</td>
      <td class="row-actions">
        <button type="button" class="btn" data-act="edit-result" data-id="${esc(item.id)}">编辑</button>
        <button type="button" class="btn" data-act="result-status" data-id="${esc(item.id)}" data-worked="${item.worked ? "0" : "1"}">${item.worked ? "标为未出作品" : "标为已出作品"}</button>
        <button type="button" class="btn danger" data-act="delete-result" data-id="${esc(item.id)}">删除</button>
        ${item.path ? `<button type="button" class="btn" data-act="open-path" data-path="${encodeURIComponent(item.path)}">打开</button>` : ""}
      </td>
    </tr>`;
  }).join("");
  root.innerHTML = `
    <div class="list-toolbar">
      <input id="result-search" placeholder="检索名称、分组、提示词" value="${esc(S.resultQuery || "")}">
      <select id="result-filter-group">
        <option value="">全部分组</option>
        ${groups.map((item) => `<option value="${esc(item)}" ${S.resultGroupFilter === item ? "selected" : ""}>${esc(item)}</option>`).join("")}
      </select>
      <select id="result-filter-tag" aria-label="筛选标签"><option value="">全部标签</option>${uniqueValues(S.results.flatMap((item) => item.tags || [])).map((tag) => `<option value="${esc(tag)}" ${S.resultTagFilter === tag ? "selected" : ""}>${esc(tag)}</option>`).join("")}</select>
      <select id="result-filter-status">
        <option value="" ${S.resultStatusFilter === "" ? "selected" : ""}>全部状态</option>
        <option value="unused" ${S.resultStatusFilter === "unused" ? "selected" : ""}>未出作品</option>
        <option value="worked" ${S.resultStatusFilter === "worked" ? "selected" : ""}>已出作品</option>
      </select>
      <button type="button" class="btn" data-act="result-batch-status" data-worked="0">勾选标为未出作品</button>
      <button type="button" class="btn" data-act="result-batch-status" data-worked="1">勾选标为已出作品</button>
      <button type="button" class="btn" data-act="result-batch-edit">批量改分组标签</button>
      <button type="button" class="btn danger" data-act="result-batch-delete">删除勾选</button>
      ${tableTransferControls("results")}
      <button type="button" class="btn" data-act="open-path" data-path="${encodeURIComponent("results")}">打开结果库</button>
      ${selectionControls("results")}
    </div>
    <div class="table-panel"><table class="data-table"><thead><tr>
      <th><input type="checkbox" data-act="check-all-results" ${allChecked ? "checked" : ""}></th>
      <th>预览</th><th>名称</th><th>分组</th><th>标签</th><th>状态</th><th>提示词</th><th>参考图</th><th>创建时间</th><th>操作</th>
    </tr></thead><tbody>${body || '<tr><td colspan="10">没有符合条件的结果，可新增作品或导入表格。</td></tr>'}</tbody></table></div>`;
  updateListSelection("results", rows, root);
  restoreFocus();
}

function openResultDialog(mode, draft) {
  S.resultDraft = draft;
  document.getElementById("result-dialog-title").textContent = mode === "add" ? "添加作品" : mode === "batch" ? "批量修改" : "编辑结果";
  document.getElementById("result-dialog-id").textContent = draft.id ? "编号 " + draft.id : mode === "batch" ? `已选 ${draft.ids.length} 张` : "保存后生成 UUID";
  const error = document.getElementById("result-dialog-error");
  if (error) error.textContent = "";
  const body = document.getElementById("result-dialog-body");
  const status = `<label class="field"><span>状态</span><select data-bind="result.worked"><option value="0" ${draft.worked ? "" : "selected"}>未出作品</option><option value="1" ${draft.worked ? "selected" : ""}>已出作品</option></select></label>`;
  if (mode === "add") {
    body.innerHTML = `
      <label class="field"><span>图片，可多选</span><input id="result-files" type="file" accept="image/*" multiple></label>
      <label class="field"><span>分组</span><input data-bind="result.group" value="${esc(draft.group || "")}"></label>
      <label class="field"><span>标签，逗号分隔</span><input data-bind="result.tags" value="${esc((draft.tags || []).join("，"))}"></label>
      ${status}`;
  } else if (mode === "batch") {
    body.innerHTML = `
      <label class="field"><span>分组，留空表示不改</span><input data-bind="result.group" value=""></label>
      <label class="field"><span>标签，留空表示不改</span><input data-bind="result.tags" value=""></label>
      <label class="field"><span>状态</span><select data-bind="result.worked"><option value="">不改</option><option value="0">未出作品</option><option value="1">已出作品</option></select></label>`;
  } else {
    body.innerHTML = `
      <label class="field"><span>名称</span><input data-bind="result.name" value="${esc(draft.name || "")}"></label>
      <label class="field"><span>分组</span><input data-bind="result.group" value="${esc(draft.group || "")}"></label>
      <label class="field"><span>标签，逗号分隔</span><input data-bind="result.tags" value="${esc((draft.tags || []).join("，"))}"></label>
      <label class="field"><span>提示词</span><textarea data-bind="result.prompt" rows="4">${esc(draft.prompt || "")}</textarea></label>
      ${status}`;
  }
  document.getElementById("result-dialog").classList.remove("hidden");
}

function closeResultDialog() {
  S.resultDraft = null;
  document.getElementById("result-dialog").classList.add("hidden");
}

function resultTags(value) {
  return String(value || "").split(/[,，]/).map((item) => item.trim()).filter(Boolean);
}

function listConfig(kind) {
  return {
    elements: { prefix: "element", checked: "elementChecked", rows: filteredElements, all: S.elements.filter((item) => item.type === S.elementTypeFilter) },
    flows: { prefix: "flow", checked: "flowChecked", rows: filteredFlows, all: S.flows },
    results: { prefix: "result", checked: "resultChecked", rows: filteredResults, all: S.results },
    runs: { prefix: "run", checked: "runChecked", rows: filteredRuns, all: S.runs },
  }[kind];
}

function selectionControls(kind) {
  return `<div class="selection-bar">
    <button type="button" class="btn" data-act="select-visible" data-kind="${kind}">全选筛选结果</button>
    <button type="button" class="btn" data-act="clear-selection" data-kind="${kind}">取消勾选</button>
    <button type="button" class="btn" data-act="reset-filters" data-kind="${kind}">重置筛选</button>
    <span class="hint" id="${listConfig(kind).prefix}-count" aria-live="polite"></span>
  </div>`;
}

function tableTransferControls(kind) {
  return `<select id="${kind}-table-format" aria-label="导出表格格式"><option value="xlsx">Excel (.xlsx)</option><option value="csv">CSV (.csv)</option></select>
    <button type="button" class="btn" data-act="import-table" data-kind="${kind}">导入表格</button>
    <button type="button" class="btn" data-act="export-table" data-kind="${kind}">导出筛选结果</button>
    <button type="button" class="btn" data-act="export-table" data-kind="${kind}" data-scope="checked">导出勾选</button>`;
}

function updateListSelection(kind, rows, root) {
  const config = listConfig(kind);
  const valid = new Set(config.all.map((item) => item.id));
  S[config.checked] = S[config.checked].filter((id) => valid.has(id));
  const checked = new Set(S[config.checked]);
  const visibleChecked = rows.filter((item) => checked.has(item.id)).length;
  const count = document.getElementById(config.prefix + "-count");
  if (count) count.textContent = `筛选 ${rows.length} / 共 ${config.all.length} 条 · 已选 ${checked.size} 条` + (checked.size > visibleChecked ? `（含隐藏 ${checked.size - visibleChecked} 条）` : "");
  const header = root.querySelector('thead input[type="checkbox"]');
  if (header) {
    header.checked = rows.length > 0 && visibleChecked === rows.length;
    header.indeterminate = visibleChecked > 0 && visibleChecked < rows.length;
    header.setAttribute("aria-label", "全选当前筛选结果");
  }
  root.querySelectorAll('tbody input[type="checkbox"]').forEach((input) => {
    input.setAttribute("aria-label", "勾选 " + input.dataset.id);
    input.closest("tr")?.classList.toggle("on", checked.has(input.dataset.id));
  });
}

function restoreListFocus(root) {
  const active = document.activeElement;
  if (!root.contains(active) || !active.id) return () => {};
  const id = active.id;
  const position = active.selectionStart;
  return () => {
    const target = document.getElementById(id);
    if (!target) return;
    target.focus();
    if (position !== null && typeof target.setSelectionRange === "function") target.setSelectionRange(position, position);
  };
}

function rerenderList(kind) {
  if (kind === "elements") { renderElementToolbar(); renderElementTable(); }
  if (kind === "flows") { renderFlowToolbar(); renderFlowList(); }
  if (kind === "results") renderResults();
  if (kind === "runs") renderRuns();
}

async function exportTable(kind, scope) {
  const config = listConfig(kind);
  const ids = scope === "checked" ? S[config.checked] : config.rows().map((item) => item.id);
  if (scope === "checked" && !ids.length) { setBanner("先勾选要导出的记录。", "bad"); return; }
  const format = document.getElementById(kind + "-table-format")?.value || "xlsx";
  const response = await fetch("/api/tables/export", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind, type: S.elementTypeFilter, ids, format }),
  });
  if (!response.ok) throw new Error((await response.json()).error || "表格导出失败");
  const blob = await response.blob();
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = (kind === "elements" ? elementTypeLabel(S.elementTypeFilter) : PAGES[{ flows: "flow", results: "results", runs: "runs" }[kind]][1]) + "." + format;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  setBanner(`已导出 ${ids.length} 条记录。`, "ok");
}

async function importTable(file) {
  const kind = S.tableImport;
  const elementType = S.tableImportType || "";
  const query = new URLSearchParams({ kind, type: elementType, name: file.name });
  const response = await fetch("/api/tables/import?" + query, { method: "POST", body: file });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "表格导入失败");
  await load();
  if (kind === "results") await loadResults();
  if (kind === "runs") await loadRuns();
  render();
  setBanner(`导入完成，新增 ${data.created} 条，更新 ${data.updated} 条。`, "ok");
}

async function setLibraryStatus(kind, ids, status) {
  const list = (ids || []).filter(Boolean);
  if (!list.length) { setBanner("先勾选。", "bad"); return; }
  if (kind === "elements") {
    await api("POST", "/api/elements/batch", { action: "status", ids: list, status });
    list.forEach((id) => {
      const element = S.elements.find((item) => item.id === id);
      if (element) element.status = status;
    });
    renderElementTable();
  } else {
    await api("POST", "/api/flows/batch", { action: "status", ids: list, status });
    list.forEach((id) => {
      const flow = S.flows.find((item) => item.id === id);
      if (flow) flow.status = status;
      if (S.flow?.id === id) S.flow.status = status;
    });
    renderFlowList();
  }
  setBanner(`已改为${status}。`, "ok");
}

async function deleteCheckedRuns(selectedIds) {
  const ids = selectedIds || S.runChecked || [];
  if (!ids.length) { setBanner("先勾选记录。", "bad"); return; }
  if (!await confirmAction(`删除 ${ids.length} 条运行记录？`)) return;
  await api("POST", "/api/runs/batch", { action: "delete", ids });
  S.runChecked = S.runChecked.filter((id) => !ids.includes(id));
  await loadRuns();
  renderRuns();
  setBanner("已删除运行记录。", "ok");
}

async function loadRuns() {
  const data = await api("GET", "/api/runs?limit=0");
  S.runs = data.runs || [];
}

function onField(event) {
  if (event.target.dataset.executionKey) {
    S.executionDraft[event.target.dataset.executionKey] = event.target.value === "" ? "" : Number(event.target.value);
    document.getElementById("execution-config-error").textContent = "";
    updateExecutionPreview(); return;
  }
  if (event.target.closest("#batch-run-dialog")) {
    document.getElementById("batch-run-error").textContent = "";
    updateBatchRunSummary();
    return;
  }
  if (["flow-copy-source", "flow-copy-count"].includes(event.target.id)) {
    document.getElementById("flow-copy-error").textContent = "";
    updateFlowCopySummary();
    return;
  }
  if (event.target.id === "reference-zip-file") {
    document.getElementById("reference-zip-error").textContent = "";
    return;
  }
  if (event.type === "change" && ["flow-search", "element-search", "result-search", "run-search"].includes(event.target.id)) return;
  if (event.target.id === "flow-search") {
    S.flowQuery = event.target.value;
    renderFlowList();
    return;
  }
  if (event.target.id === "flow-filter-group" || event.target.id === "flow-filter-tag" || event.target.id === "flow-sort") {
    S.flowGroupFilter = document.getElementById("flow-filter-group")?.value || "";
    S.flowTagFilter = document.getElementById("flow-filter-tag")?.value || "";
    S.flowSort = document.getElementById("flow-sort")?.value || "updated-desc";
    renderFlowList();
    return;
  }
  if (event.target.dataset.act === "check-flow") {
    const checked = new Set(S.flowChecked || []);
    if (event.target.checked) checked.add(event.target.dataset.id);
    else checked.delete(event.target.dataset.id);
    S.flowChecked = [...checked];
    renderFlowList();
    return;
  }
  if (event.target.dataset.act === "check-all-flows") {
    const visible = filteredFlows().map((item) => item.id);
    if (event.target.checked) S.flowChecked = [...new Set([...(S.flowChecked || []), ...visible])];
    else S.flowChecked = (S.flowChecked || []).filter((id) => !visible.includes(id));
    renderFlowList();
    return;
  }
  if (event.target.id === "api-keys" && S.elementDraft) {
    S.elementDraft._apiKeys = event.target.value;
    return;
  }
  if (event.target.id === "ref-file") {
    if (event.type === "change") uploadImage().catch((error) => showElementError(error.message));
    return;
  }
  if (event.target.id === "element-search") {
    S.elementQuery = event.target.value;
    renderElementTable();
    return;
  }
  if (event.target.id === "element-filter-type" || event.target.id === "element-filter-group" || event.target.id === "element-filter-tag" || event.target.id === "element-filter-status") {
    S.elementGroupFilter = document.getElementById("element-filter-group")?.value || "";
    S.elementTagFilter = document.getElementById("element-filter-tag")?.value || "";
    S.elementStatusFilter = document.getElementById("element-filter-status")?.value || "";
    renderElementTable();
    return;
  }
  if (event.target.id === "result-search") {
    S.resultQuery = event.target.value;
    renderResults();
    return;
  }
  if (["result-filter-group", "result-filter-status", "result-filter-tag"].includes(event.target.id)) {
    S.resultGroupFilter = document.getElementById("result-filter-group")?.value || "";
    S.resultStatusFilter = document.getElementById("result-filter-status")?.value || "";
    S.resultTagFilter = document.getElementById("result-filter-tag")?.value || "";
    renderResults();
    return;
  }
  if (event.target.id === "flow-filter-status") {
    S.flowStatusFilter = event.target.value || "";
    renderFlowList();
    return;
  }
  if (event.target.id === "run-search") {
    S.runQuery = event.target.value;
    renderRuns();
    return;
  }
  if (["run-filter-status", "run-filter-flow", "run-filter-mode"].includes(event.target.id)) {
    S.runStatusFilter = document.getElementById("run-filter-status")?.value || "";
    S.runFlowFilter = document.getElementById("run-filter-flow")?.value || "";
    S.runModeFilter = document.getElementById("run-filter-mode")?.value || "";
    renderRuns();
    return;
  }
  if (event.target.dataset.act === "check-run") {
    const id = event.target.dataset.id;
    const picked = new Set(S.runChecked || []);
    if (event.target.checked) picked.add(id);
    else picked.delete(id);
    S.runChecked = [...picked];
    updateListSelection("runs", filteredRuns(), document.getElementById("runs"));
    return;
  }
  if (event.target.dataset.act === "check-all-runs") {
    const visible = filteredRuns().map((item) => item.id);
    if (event.target.checked) S.runChecked = [...new Set([...(S.runChecked || []), ...visible])];
    else S.runChecked = (S.runChecked || []).filter((id) => !visible.includes(id));
    renderRuns();
    return;
  }
  if (event.target.dataset.act === "check-all-results") {
    const visible = filteredResults().map((item) => item.id);
    if (event.target.checked) S.resultChecked = [...new Set([...(S.resultChecked || []), ...visible])];
    else S.resultChecked = (S.resultChecked || []).filter((id) => !visible.includes(id));
    renderResults();
    return;
  }
  if (event.target.dataset.act === "check-result") {
    const id = event.target.dataset.id;
    const picked = new Set(S.resultChecked || []);
    if (event.target.checked) picked.add(id);
    else picked.delete(id);
    S.resultChecked = [...picked];
    updateListSelection("results", filteredResults(), document.getElementById("results"));
    return;
  }
  if (event.target.dataset.act === "check-element") {
    const id = event.target.dataset.id;
    const checked = new Set(S.elementChecked || []);
    if (event.target.checked) checked.add(id);
    else checked.delete(id);
    S.elementChecked = [...checked];
    renderElementTable();
    return;
  }
  if (event.target.dataset.act === "check-all-elements") {
    const visible = filteredElements().map((item) => item.id);
    if (event.target.checked) S.elementChecked = [...new Set([...(S.elementChecked || []), ...visible])];
    else S.elementChecked = (S.elementChecked || []).filter((id) => !visible.includes(id));
    renderElementTable();
    return;
  }
  const el = event.target.closest("[data-bind]");
  if (!el) {
    if (event.target.dataset.act === "switch-flow") selectFlow(event.target.value);
    return;
  }
  const bind = el.dataset.bind;
  if (bind === "flow-meta.name" || bind === "flow-meta.description" || bind === "flow-meta.group" || bind === "flow-meta.tags" || bind === "flow-field") {
    if (!S.flowDraft) return;
  }
  if (bind === "flow-meta.name") {
    S.flowDraft.name = el.value;
    return;
  }
  if (bind === "flow-meta.description") {
    S.flowDraft.description = el.value;
    return;
  }
  if (bind === "flow-meta.group") {
    S.flowDraft.group = el.value;
    return;
  }
  if (bind === "flow-meta.tags") {
    S.flowDraft.tags = String(el.value).split(/[,，]/).map((item) => item.trim()).filter(Boolean);
    return;
  }
  if (bind === "flow-field" && S.flowDraft) {
    const item = S.flowDraft.fields[Number(el.dataset.index)];
    if (item) item[el.dataset.key] = el.value;
    return;
  }
  if (bind === "flow.name") {
    S.flow.name = el.value;
    const listed = S.flows.find((item) => item.id === S.flow.id);
    if (listed) listed.name = el.value;
    const title = document.getElementById("page-title");
    const crumb = document.getElementById("crumb-page");
    if (S.editing && title) title.textContent = el.value || "未命名流程";
    if (S.editing && crumb) crumb.textContent = el.value || "未命名流程";
    touch();
    return;
  }
  if (bind === "flow.description") {
    S.flow.description = el.value;
    touch();
    return;
  }
  if (bind === "schedule.enabled") {
    S.flow.schedule.enabled = el.checked;
    touch();
    return;
  }
  if (bind === "schedule.frequency") {
    S.flow.schedule.frequency = el.value;
    renderInspector();
    touch();
    return;
  }
  if (bind === "schedule.time") {
    S.flow.schedule.time = el.value || "09:00";
    touch();
    return;
  }
  if (bind === "schedule.date") {
    S.flow.schedule.date = el.value;
    touch();
    return;
  }
  if (bind === "schedule.weekday") {
    const day = Number(el.dataset.day);
    const days = new Set(S.flow.schedule.weekdays || []);
    if (el.checked) days.add(day);
    else days.delete(day);
    S.flow.schedule.weekdays = [...days].sort((a, b) => a - b);
    touch();
    return;
  }
  const node = S.selected?.kind === "node" ? findNode(S.selected.id) : null;
  if (bind.startsWith("parameter.") && node) {
    const item = node.params?.carryParams?.[Number(el.dataset.index)];
    if (!item) return;
    if (bind === "parameter.target") item.target = el.value;
    else if (bind === "parameter.value") item.value = el.value;
    else if (bind === "parameter.customSource") item.source = el.value;
    else {
      if (el.value === "__literal__") {
        item.sourceType = "literal";
        item.value ??= "";
      } else {
        delete item.sourceType;
        item.source = el.value === "__custom__" ? "" : el.value;
      }
      renderInspector();
    }
    touch();
    return;
  }
  if (bind === "node.nameTemplate" && node) {
    node.params.nameTemplate = el.value;
    touch();
    return;
  }
  if (bind === "node.sizePreset" && node) {
    const previous = S.imageSizePresets.find(item => item.id === imageSizePreset(node));
    node.params.sizePreset = el.value;
    if (el.value === 'custom') {
      node.params.imageWidth ??= previous?.width || 1080;
      node.params.imageHeight ??= previous?.height || 1920;
    }
    touch();
    renderInspector();
    return;
  }
  if ((bind === "node.imageWidth" || bind === "node.imageHeight") && node) {
    node.params[bind === "node.imageWidth" ? 'imageWidth' : 'imageHeight'] = el.value === '' ? '' : Number(el.value);
    touch();
    return;
  }
  if (bind === "node.name" && node) {
    node.name = el.value;
    const title = document.querySelector(`.node[data-id="${CSS.escape(node.id)}"] .node-name`);
    if (title) title.textContent = el.value;
    touch();
    return;
  }
  if (bind === "node.themeText" && node) {
    node.params.themeText = el.value;
    touch();
    return;
  }
  if (bind === "node.keepInlet" && node) {
    node.params.keepInlet = el.checked;
    touch();
    return;
  }
  if (bind === "node.useModel" && node) {
    node.params.useModel = el.checked;
    touch();
    return;
  }
  if (bind === "node.contextMode" && node) {
    node.params.contextMode = el.value;
    touch();
    return;
  }
  if (bind === "node.count" && node) {
    const count = Math.max(1, Math.min(12, Number(el.value) || 1));
    node.params.count = count;
    touch();
    return;
  }
  if (bind === "node.referenceCount" && node) {
    node.params.referenceCount = el.value === "" ? "" : Number(el.value);
    updateNodeImportSummary(node);
    touch();
    return;
  }
  if (bind === "node.purpose" && node) {
    node.params.purpose = el.value;
    touch();
    return;
  }
  if (bind === "node.directory" && node) {
    node.params.directory = el.value.trim();
    touch();
    return;
  }
  if (bind === "node.saveAs" && node) {
    node.params.saveAs = el.value;
    touch();
    return;
  }
  if (bind === "node.resultGroup" && node) {
    node.params.group = el.value;
    touch();
    return;
  }
  if (bind === "node.zipName" && node) {
    node.params.zipName = el.value;
    touch();
    return;
  }
  if ((bind === "node.refGroup" || bind === "node.refTag") && node) {
    node.params[bind === "node.refGroup" ? "refGroup" : "refTag"] = el.value;
    touch();
    renderInspector();
    return;
  }
  if (bind === "import" && node) {
    node.imports = (node.imports || []).filter((item) => item.elementId !== el.dataset.elementId);
    if (el.checked) {
      node.imports.push({ alias: el.dataset.elementType, elementType: el.dataset.elementType, elementId: el.dataset.elementId });
    }
    updateNodeImportSummary(node);
    touch();
    return;
  }
  if ((bind === "rulesBefore" || bind === "rulesAfter") && node) {
    const list = new Set(node[bind] || []);
    if (el.checked) list.add(el.dataset.ruleId);
    else list.delete(el.dataset.ruleId);
    node[bind] = [...list];
    touch();
    return;
  }
  const edge = S.selected?.kind === "edge" ? findEdge(S.selected.id) : null;
  if (edge && bind.startsWith("carry.")) {
    const mapping = edge.carry[Number(el.dataset.index)];
    if (!mapping) return;
    const key = bind.slice("carry.".length);
    mapping[key] = el.value;
    if (key === "op") renderInspector();
    drawWires();
    touch();
    return;
  }
  if (bind === "edge.rules" && edge) {
    const list = new Set(edge.rules || []);
    if (el.checked) list.add(el.dataset.ruleId);
    else list.delete(el.dataset.ruleId);
    edge.rules = [...list];
    touch();
    return;
  }
  const element = S.elementDraft || currentElement();
  if (!element) return;
  const inDialog = Boolean(S.elementDraft);
  if (bind === "element.name") {
    element.name = el.value;
    if (!inDialog) touchElement();
    return;
  }
  if (bind === "element.group") {
    element.group = el.value;
    return;
  }
  if (bind === "element.status") {
    element.status = el.value || "启用";
    return;
  }
  if (bind === "flow-meta.status") {
    if (S.flowDraft) S.flowDraft.status = el.value || "启用";
    return;
  }
  if (bind === "element.tags") {
    element.tags = String(el.value).split(/[,，]/).map((item) => item.trim()).filter(Boolean);
    return;
  }
  if (bind === "element.type" && inDialog && !element.id) {
    syncElementDraftFromDom();
    element.type = el.value;
    element.payload = clone(S.elementDefaults[element.type] || {});
    renderElementDialog();
    return;
  }
  if (bind === "custom-field") {
    const item = element.fields[Number(el.dataset.index)];
    if (item) item[el.dataset.key] = el.value;
    return;
  }
  if (bind.startsWith("payload.")) {
    const key = bind.slice("payload.".length);
    let value = el.type === "checkbox" ? el.checked : el.value;
    if (key === "tags") value = String(el.value).split(/[,，]/).map((item) => item.trim()).filter(Boolean);
    if (key === "temperature") value = Number(el.value);
    if (key === "maxTokens" || key === "concurrency") value = el.value === "" ? null : Number(el.value);
    element.payload[key] = value;
    if ((key === "kind" || key === "purpose") && inDialog) {
      syncElementDraftFromDom();
      renderElementDialog();
    }
    if (!inDialog) touchElement();
    return;
  }
  if (bind === "variant") {
    const variant = element.payload.variants[Number(el.dataset.index)];
    const key = el.dataset.key;
    variant[key] = ["width", "height", "count"].includes(key) ? Number(el.value || 0) : el.value;
    if (!inDialog) touchElement();
    return;
  }
  if (bind === "step") {
    const step = element.payload.steps[Number(el.dataset.index)];
    step[el.dataset.key] = el.value;
    if (!inDialog) touchElement();
  }
}

async function onClick(event) {
  const inDialog = Boolean(event.target.closest(".modal-backdrop, dialog[open]"));
  if (S.suppressClick && !inDialog) {
    S.suppressClick = false;
    return;
  }
  S.suppressClick = false;
  const winBtn = event.target.closest("[data-window]");
  if (winBtn && window.desktop?.window) {
    window.desktop.window(winBtn.dataset.window);
    return;
  }
  const moduleBtn = event.target.closest("#element-nav [data-element-type]");
  if (moduleBtn) {
    try {
      await persistNow();
      await flushElement();
    } catch (error) {
      setBanner(error.message, "bad");
      return;
    }
    S.tab = "elements";
    S.elementNavOpen = true;
    S.elementTypeFilter = moduleBtn.dataset.elementType;
    S.elementGroupFilter = "";
    S.elementTagFilter = "";
    S.elementStatusFilter = "";
    S.elementQuery = "";
    S.elementChecked = [];
    render();
    return;
  }
  const tab = event.target.closest("[data-tab]");
  if (tab) {
    try {
      await persistNow();
      await flushElement();
    } catch (error) {
      setBanner(error.message, "bad");
      return;
    }
    if (tab.dataset.tab === "flow") S.editing = false;
    if (tab.dataset.tab === "elements") {
      S.elementNavOpen = S.tab === "elements" ? !S.elementNavOpen : true;
      ensureElementModule();
    }
    S.tab = tab.dataset.tab;
    if (S.tab === "runs") {
      try { await loadRuns(); } catch (error) { setBanner(error.message, "bad"); }
    }
    if (S.tab === "results") {
      try { await loadResults(); } catch (error) { setBanner(error.message, "bad"); }
    }
    render();
    return;
  }
  const act = event.target.closest("[data-act]");
  if (!act) {
    if (event.target.id === "board" || event.target.id === "board-wrap" || event.target.id === "wires") {
      S.selected = null;
      S.linking = null;
      renderBoard();
      renderInspector();
    }
    return;
  }
  const action = act.dataset.act;
  if (action === "open-execution-config") {
    closeBatchRun(); S.tab = "execution"; render(); return;
  }
  if (action === "save-execution-config") return saveExecutionConfig();
  if (action === "reset-execution-config") {
    S.executionDraft = { flowConcurrency: 3, promptConcurrency: 6, imageConcurrency: 6 };
    renderExecutionConfig(); setBanner("已填入默认值，保存后生效。", ""); return;
  }
  if (action === "open-batch-run") return openBatchRun();
  if (action === "close-batch-run") return closeBatchRun();
  if (action === "submit-batch-run") return submitBatchRun();
  if (action === "cancel-batch-run") {
    await api("POST", "/api/batch-runs/" + encodeURIComponent(act.dataset.id) + "/cancel", {});
    return refreshBatchRuns();
  }
  if (action === "add-parameter" || action === "delete-parameter") {
    const node = S.selected?.kind === "node" ? findNode(S.selected.id) : null;
    if (!node) return;
    const bindings = node.params.carryParams || (node.params.carryParams = []);
    if (action === "add-parameter") bindings.push({ source: node.type === "prompt.from_multi_refs" ? "referenceNotes" : node.type === "prompt.from_refs" ? "referenceUsed.name" : "title", target: "title" });
    else bindings.splice(Number(act.dataset.index), 1);
    renderInspector();
    touch();
    return;
  }
  if (action === "select-node-references") return selectNodeReferences(true);
  if (action === "clear-node-references") return selectNodeReferences(false);
  if (action === "copy-flow") return openFlowCopy(act.dataset.id);
  if (action === "submit-flow-copy") return copyFlows();
  if (action === "close-flow-copy") {
    if (!S.flowCopyBusy) {
      document.getElementById("flow-copy-dialog").classList.add("hidden");
      S.flowCopyIds = [];
    }
    return;
  }
  if (action === "open-reference-zip") return openReferenceZip();
  if (action === "submit-reference-zip") return importReferenceZip();
  if (action === "close-reference-zip") {
    if (!S.referenceZipBusy) document.getElementById("reference-zip-dialog").classList.add("hidden");
    return;
  }
  if (action === "select-visible" || action === "clear-selection") {
    const kind = act.dataset.kind;
    const config = listConfig(kind);
    S[config.checked] = action === "clear-selection" ? [] : [...new Set([...S[config.checked], ...config.rows().map((item) => item.id)])];
    rerenderList(kind);
    return;
  }
  if (action === "reset-filters") {
    const kind = act.dataset.kind;
    const prefix = listConfig(kind).prefix;
    ["Query", "GroupFilter", "TagFilter", "StatusFilter", "FlowFilter", "ModeFilter"].forEach((suffix) => { if (prefix + suffix in S) S[prefix + suffix] = ""; });
    rerenderList(kind);
    return;
  }
  if (action === "add-run") return openRunEditor();
  if (action === "edit-run") return openRunEditor(S.runs.find((run) => run.id === act.dataset.id));
  if (action === "save-run-record") return saveRunEditor();
  if (action === "close-run-record") {
    document.getElementById("run-record-dialog").classList.add("hidden");
    S.runDraft = null;
    return;
  }
  if (action === "delete-run") return deleteCheckedRuns([act.dataset.id]);
  if (action === "run-batch-status") {
    if (!S.runChecked.length) { setBanner("先勾选运行记录。", "bad"); return; }
    await api("POST", "/api/runs/batch", { action: "status", ids: S.runChecked, status: act.dataset.status });
    await loadRuns();
    renderRuns();
    setBanner("已更新勾选记录的状态。", "ok");
    return;
  }
  if (action === "reload-results") { await loadResults(); renderResults(); return; }
  if (action === "select-node") {
    const changed = S.selected?.kind !== "node" || S.selected.id !== act.dataset.id;
    S.selected = { kind: "node", id: act.dataset.id };
    renderBoard();
    if (changed) renderInspector();
    return;
  }
  if (action === "select-edge") {
    S.selected = { kind: "edge", id: act.dataset.id };
    S.linking = null;
    renderBoard();
    renderInspector();
    return;
  }
  if (action === "port-out") {
    S.linking = act.dataset.id;
    S.selected = { kind: "node", id: act.dataset.id };
    setBanner("再点下一个节点的入口。", "ok");
    renderBoard();
    renderInspector();
    return;
  }
  if (action === "port-in") {
    if (!S.linking) {
      S.selected = { kind: "node", id: act.dataset.id };
      renderBoard();
      renderInspector();
      return;
    }
    connect(S.linking, act.dataset.id);
    return;
  }
  if (action === "open-flow") return selectFlow(act.dataset.id);
  if (action === "format-dag") return formatDag();
  if (action === "zoom-in") return setZoom((S.zoom || 1) + 0.1);
  if (action === "zoom-out") return setZoom((S.zoom || 1) - 0.1);
  if (action === "zoom-reset") return setZoom(1);
  if (action === "edit-connection") return openConnectionDialog(S.selected?.id, false);
  if (action === "confirm-connection") return confirmConnection();
  if (action === "cancel-connection") return cancelConnection();
  if (action === "back-flows") return leaveEditor();
  if (action === "add-node") return addNode(act.dataset.type);
  if (action === "add-carry") return addCarry();
  if (action === "delete-carry") return deleteCarry(Number(act.dataset.index));
  if (action === "delete-selected") return confirmDeleteSelection();
  if (action === "duplicate-node") return duplicateNode();
  if (action === "new-flow") return createFlow();
  if (action === "delete-flow") return deleteFlow();
  if (action === "open-run") {
    document.getElementById("run-output-dir").value = outputDirectory();
    const outputNode = (S.flow?.graph.nodes || []).find((item) => item.type === "work.generate") || (S.flow?.graph.nodes || []).find((item) => item.type === "file.output");
    const isWork = outputNode?.type === "work.generate";
    const saveSelect = document.getElementById("run-save-as");
    if (saveSelect) {
      saveSelect.value = isWork ? "zip" : outputNode?.params?.saveAs || "";
      saveSelect.disabled = isWork;
    }
    document.getElementById("run-dialog").classList.remove("hidden");
    document.getElementById("run-output-dir").focus();
    return;
  }
  if (action === "pick-output-dir") return pickOutputDirectory(act.dataset.target);
  if (action === "export-flow") return exportFlow();
  if (action === "export-library") return exportLibrary();
  if (action === "import") {
    document.getElementById("import-file").click();
    return;
  }
  if (action === "check-element" || action === "check-all-elements" || action === "check-flow" || action === "check-all-flows" || action === "check-result" || action === "check-all-results" || action === "check-run" || action === "check-all-runs") return;
  if (action === "new-flow-meta") return openFlowMeta({ id: "", name: "", description: "", group: S.flowGroupFilter || "", tags: S.flowTagFilter ? [S.flowTagFilter] : [], fields: [] });
  if (action === "edit-flow-meta") {
    const flow = S.flows.find((item) => item.id === (act.dataset.id || S.flow?.id));
    if (flow) openFlowMeta(flow);
    return;
  }
  if (action === "add-flow-field") {
    syncFlowDraftFromDom();
    S.flowDraft.fields.push({ key: "", value: "" });
    renderFlowDialog();
    return;
  }
  if (action === "delete-flow-field") {
    syncFlowDraftFromDom();
    S.flowDraft.fields.splice(Number(act.dataset.index), 1);
    renderFlowDialog();
    return;
  }
  if (action === "save-flow-meta") return saveFlowMeta();
  if (action === "close-flow-meta") return closeFlowMeta();
  if (action === "delete-one-flow") return deleteFlow(act.dataset.id);
  if (action === "delete-checked-flows") return deleteCheckedFlows();
  if (action === "export-flows") return exportFlows();
  if (action === "export-checked-flows") return exportFlows(S.flowChecked || []);
  if (action === "add-result") {
    openResultDialog("add", { mode: "add", group: S.resultGroupFilter || "", tags: [], worked: false });
    return;
  }
  if (action === "edit-result") {
    const item = (S.results || []).find((row) => row.id === act.dataset.id);
    if (item) openResultDialog("edit", { ...item, mode: "edit" });
    return;
  }
  if (action === "result-batch-edit") {
    const ids = S.resultChecked || [];
    if (!ids.length) { setBanner("先勾选结果图。", "bad"); return; }
    openResultDialog("batch", { mode: "batch", ids, worked: "" });
    return;
  }
  if (action === "close-result") return closeResultDialog();
  if (action === "save-result") return saveResultDialog();
  if (action === "delete-result") return deleteResults([act.dataset.id]);
  if (action === "result-batch-delete") return deleteResults(S.resultChecked || []);
  if (action === "result-status") return setResultStatus([act.dataset.id], act.dataset.worked === "1");
  if (action === "result-batch-status") return setResultStatus(S.resultChecked || [], act.dataset.worked === "1");
  if (action === "element-batch-status") return setLibraryStatus("elements", S.elementChecked || [], act.dataset.status);
  if (action === "flow-batch-status") return setLibraryStatus("flows", S.flowChecked || [], act.dataset.status);
  if (action === "export-table") return exportTable(act.dataset.kind, act.dataset.scope);
  if (action === "import-table") {
    S.tableImport = act.dataset.kind;
    S.tableImportType = S.elementTypeFilter;
    document.getElementById("table-file").click();
    return;
  }
  if (action === "delete-checked-runs") return deleteCheckedRuns();
  if (action === "edit-element") {
    const element = S.elements.find((item) => item.id === act.dataset.id);
    if (element) openElementEditor(element);
    return;
  }
  if (action === "new-element-any") return openNewElement();
  if (action === "add-typed") {
    S.tab = "elements";
    S.elementNavOpen = true;
    S.elementTypeFilter = act.dataset.type;
    S.elementGroupFilter = "";
    S.elementTagFilter = "";
    S.elementChecked = [];
    S.elementStatusFilter = "";
    S.elementQuery = "";
    render();
    openNewElement();
    return;
  }
  if (action === "add-custom-field") {
    syncElementDraftFromDom();
    S.elementDraft.fields.push({ key: "", value: "" });
    renderElementDialog();
    return;
  }
  if (action === "delete-custom-field") {
    syncElementDraftFromDom();
    S.elementDraft.fields.splice(Number(act.dataset.index), 1);
    renderElementDialog();
    return;
  }
  if (action === "save-element") return saveElementDraft();
  if (action === "close-element") return closeElementEditor();
  if (action === "delete-one") return deleteElement(act.dataset.id);
  if (action === "delete-checked") return deleteChecked();
  if (action === "export-checked") return exportChecked();
  if (action === "new-element") return createElement(act.dataset.type);
  if (action === "delete-element") return deleteElement();
  if (action === "add-step") return mutateElement(() => activeRecord().payload.steps.push({ op: "require", field: "", from: "", template: "", value: "", message: "" }));
  if (action === "delete-step") return mutateElement(() => activeRecord().payload.steps.splice(Number(act.dataset.index), 1));
  if (action === "add-variant") return mutateElement(() => activeRecord().payload.variants.push({ name: "新配置", width: 1080, height: 1920, count: 1, style: "", negative: "" }));
  if (action === "delete-variant") return mutateElement(() => activeRecord().payload.variants.splice(Number(act.dataset.index), 1));
  if (action === "upload-image") return uploadImage();
  if (action === "clear-key") return saveKey(true);
  if (action === "remove-key") return removeKey(Number(act.dataset.index));
  if (action === "test-model") return testModel();
  if (action === "open-path") return openPath(act.dataset.path);
  if (action === "reload-runs") {
    await loadRuns();
    renderRuns();
  }
}

function wouldCycle(fromId, toId, ignoreEdgeId) {
  const next = {};
  for (const edge of S.flow.graph.edges) {
    if (edge.id === ignoreEdgeId) continue;
    if (!next[edge.from]) next[edge.from] = [];
    next[edge.from].push(edge.to);
  }
  const seen = new Set();
  const stack = [toId];
  while (stack.length) {
    const current = stack.pop();
    if (current === fromId) return true;
    if (seen.has(current)) continue;
    seen.add(current);
    for (const item of next[current] || []) stack.push(item);
  }
  return false;
}

function connect(fromId, toId) {
  if (fromId === toId) {
    S.linking = null;
    setBanner("节点不能连接自己。", "bad");
    renderBoard();
    return;
  }
  if (wouldCycle(fromId, toId)) {
    S.linking = null;
    setBanner("这样会形成环路。流程需要是有向无环图，下游不能再指回上游。", "bad");
    renderBoard();
    return;
  }
  const existing = S.flow.graph.edges.find((edge) => edge.from === fromId && edge.to === toId);
  if (existing) {
    S.selected = { kind: "edge", id: existing.id };
    S.linking = null;
    renderBoard();
    renderInspector();
    return;
  }
  const from = findNode(fromId);
  const to = findNode(toId);
  const edge = {
    id: newId("edge"),
    from: fromId,
    to: toId,
    carry: defaultMappings(from.type, to.type),
    rules: [],
  };
  S.flow.graph.edges.push(edge);
  S.linking = null;
  S.selected = { kind: "edge", id: edge.id };
  renderBoard();
  renderInspector();
  openConnectionDialog(edge.id, true);
}

function candidateFields(fromType, toType) {
  const src = typeInfo(fromType);
  const dst = typeInfo(toType);
  const available = new Set([...(src.product || []), ...(src.forward || [])]);
  const fields = (dst.accepts || []).filter((name) => available.has(name));
  if (!fields.length && src.product?.[0]) fields.push(src.product[0]);
  return fields;
}

function openConnectionDialog(edgeId, isNew) {
  const edge = findEdge(edgeId);
  if (!edge) return;
  const from = findNode(edge.from);
  const to = findNode(edge.to);
  const fields = candidateFields(from?.type, to?.type);
  const chosen = new Set((edge.carry || []).filter((item) => (item.op || "copy") === "copy" && item.source === item.target).map((item) => item.target));
  document.getElementById("conn-source").textContent = from?.name || edge.from;
  document.getElementById("conn-target").textContent = to?.name || edge.to;
  document.getElementById("conn-fields").innerHTML = fields.length
    ? fields.map((field) => `<label class="check"><input type="checkbox" value="${esc(field)}" ${chosen.has(field) ? "checked" : ""}> ${esc(field)}</label>`).join("")
    : '<p class="hint">没有普通字段可选，下游等上游完成后执行。</p>';
  document.getElementById("conn-fields").insertAdjacentHTML("beforeend", '<p class="hint">节点设置的携带参数（例如 title）会自动沿这条连线继续传递。</p>');
  S.pendingEdge = edgeId;
  S.pendingIsNew = Boolean(isNew);
  const confirm = document.querySelector("#conn-dialog [data-act='confirm-connection']");
  if (confirm) confirm.textContent = isNew ? "创建" : "确定";
  document.getElementById("conn-dialog").classList.remove("hidden");
}

function confirmConnection() {
  const edge = findEdge(S.pendingEdge);
  if (edge) {
    const available = new Set([...document.querySelectorAll("#conn-fields input")].map((box) => box.value));
    const custom = (edge.carry || []).filter((item) => (item.op || "copy") !== "copy" || item.source !== item.target || !available.has(item.target));
    edge.carry = [...document.querySelectorAll("#conn-fields input:checked")].map((box) => ({
      source: box.value,
      target: box.value,
      op: "copy",
    })).concat(custom);
    S.selected = { kind: "edge", id: edge.id };
  }
  S.pendingEdge = null;
  S.pendingIsNew = false;
  document.getElementById("conn-dialog").classList.add("hidden");
  renderBoard();
  renderInspector();
  touch();
}

function cancelConnection() {
  if (S.pendingIsNew && S.pendingEdge) {
    S.flow.graph.edges = S.flow.graph.edges.filter((edge) => edge.id !== S.pendingEdge);
    if (S.selected?.id === S.pendingEdge) S.selected = null;
  }
  S.pendingEdge = null;
  S.pendingIsNew = false;
  document.getElementById("conn-dialog").classList.add("hidden");
  renderBoard();
  renderInspector();
}

function formatDag() {
  if (!S.flow) return;
  const nodes = S.flow.graph.nodes || [];
  const edges = S.flow.graph.edges || [];
  const incoming = {};
  const outgoing = {};
  const predecessors = {};
  nodes.forEach((node) => {
    incoming[node.id] = 0;
    outgoing[node.id] = [];
    predecessors[node.id] = [];
  });
  for (const edge of edges) {
    if (incoming[edge.to] === undefined || outgoing[edge.from] === undefined) continue;
    incoming[edge.to] += 1;
    outgoing[edge.from].push(edge.to);
    predecessors[edge.to].push(edge.from);
  }
  let layer = nodes.filter((node) => incoming[node.id] === 0).map((node) => node.id);
  const depth = {};
  let level = 0;
  const seen = new Set();
  while (layer.length) {
    layer.forEach((id) => { depth[id] = level; });
    const next = [];
    for (const id of layer) {
      if (seen.has(id)) continue;
      seen.add(id);
      for (const target of outgoing[id] || []) {
        incoming[target] -= 1;
        if (incoming[target] === 0) next.push(target);
      }
    }
    layer = next;
    level += 1;
    if (level > nodes.length + 2) break;
  }
  const byId = Object.fromEntries(nodes.map((node) => [node.id, node]));
  const maxColumn = nodes.reduce((max, node) => Math.max(max, depth[node.id] ?? 0), 0);
  let tallest = 1;
  for (let column = 0; column <= maxColumn; column += 1) {
    const columnNodes = nodes.filter((node) => (depth[node.id] ?? 0) === column);
    columnNodes.sort((a, b) => {
      const ay = predecessorY(a.id);
      const by = predecessorY(b.id);
      if (ay !== by) return ay - by;
      return (a.name || "").localeCompare(b.name || "", "zh");
    });
    tallest = Math.max(tallest, columnNodes.length);
    columnNodes.forEach((node, index) => {
      node.x = 64 + column * 300;
      node.y = index;
    });
  }
  nodes.forEach((node) => {
    const columnCount = nodes.filter((item) => (depth[item.id] ?? 0) === (depth[node.id] ?? 0)).length;
    const offset = ((tallest - columnCount) * 160) / 2;
    node.y = 56 + offset + node.y * 160;
  });
  renderBoard();
  const wrap = document.getElementById("board-wrap");
  if (wrap) wrap.scrollTo({ left: 0, top: 0 });
  setBanner("已按上下游从左到右排好。", "ok");
  touch();

  function predecessorY(id) {
    const list = predecessors[id] || [];
    if (!list.length) return byId[id].y || 0;
    return list.reduce((sum, item) => sum + (byId[item].y || 0), 0) / list.length;
  }
}

function addNode(type, point) {
  if (!S.flow) return;
  const info = typeInfo(type);
  const nodes = S.flow.graph.nodes;
  const x = point ? Math.max(8, point.x - 110) : nodes.reduce((max, item) => Math.max(max, (item.x || 0) + 280), 48);
  const y = point ? Math.max(8, point.y - 36) : 150 + (nodes.length % 3) * 40;
  const node = {
    id: newId("node"),
    type,
    name: info.label,
    x,
    y,
    imports: [],
    rulesBefore: [],
    rulesAfter: [],
    params: { keepInlet: !["start.theme", "file.output", "work.generate", "result.direct"].includes(type), ...clone(info.defaults || {}) },
  };
  if (type === 'image.generate') node.params.sizePreset = imageSizePreset(node);
  nodes.push(node);
  S.selected = { kind: "node", id: node.id };
  renderBoard();
  renderInspector();
  const wrap = document.getElementById("board-wrap");
  if (wrap) wrap.scrollTo({ left: Math.max(0, x - 80), top: Math.max(0, y - 48), behavior: "smooth" });
  touch();
}

function addCarry() {
  const edge = findEdge(S.selected?.id);
  if (!edge) return;
  edge.carry = edge.carry || [];
  edge.carry.push({ source: "", target: "", op: "copy" });
  renderInspector();
  touch();
}

function deleteCarry(index) {
  const edge = findEdge(S.selected?.id);
  if (!edge) return;
  edge.carry.splice(index, 1);
  renderInspector();
  drawWires();
  touch();
}

async function confirmDeleteSelection() {
  if (!S.selected) return;
  const label = S.selected.kind === "node" ? "删除这个节点和它的连线？" : "删除这条连线？";
  if (!await confirmAction(label)) return;
  if (S.selected.kind === "node") {
    const id = S.selected.id;
    S.flow.graph.nodes = S.flow.graph.nodes.filter((node) => node.id !== id);
    S.flow.graph.edges = S.flow.graph.edges.filter((edge) => edge.from !== id && edge.to !== id);
  } else {
    S.flow.graph.edges = S.flow.graph.edges.filter((edge) => edge.id !== S.selected.id);
  }
  S.selected = null;
  renderBoard();
  renderInspector();
  touch();
}

function duplicateNode() {
  const node = findNode(S.selected?.id);
  if (!node) return;
  const copy = clone(node);
  copy.id = newId("node");
  copy.x = (node.x || 0) + 32;
  copy.y = (node.y || 0) + 32;
  copy.name = node.name + " 副本";
  S.flow.graph.nodes.push(copy);
  S.selected = { kind: "node", id: copy.id };
  renderBoard();
  renderInspector();
  touch();
}

async function createFlow() {
  try { await persistNow(); } catch (error) { setBanner(error.message, "bad"); return; }
  const flow = await api("POST", "/api/flows", { name: "未命名流程" });
  S.flows.push(flow);
  S.flow = flow;
  S.selected = null;
  S.lastTrace = null;
  S.tab = "flow";
  S.editing = true;
  render();
}

function syncFlowDraftFromDom() {
  const root = document.getElementById("flow-dialog-body");
  const draft = S.flowDraft;
  if (!root || !draft) return;
  const name = root.querySelector("[data-bind='flow-meta.name']");
  if (name) draft.name = name.value;
  const description = root.querySelector("[data-bind='flow-meta.description']");
  if (description) draft.description = description.value;
  const group = root.querySelector("[data-bind='flow-meta.group']");
  if (group) draft.group = group.value;
  const tags = root.querySelector("[data-bind='flow-meta.tags']");
  if (tags) draft.tags = tags.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
  const status = root.querySelector("[data-bind='flow-meta.status']");
  if (status) draft.status = status.value;
  root.querySelectorAll("[data-bind='flow-field']").forEach((el) => {
    const item = (draft.fields || [])[Number(el.dataset.index)];
    if (item) item[el.dataset.key] = el.value;
  });
}

async function saveFlowMeta() {
  const draft = S.flowDraft;
  if (!draft) return;
  const errorEl = document.getElementById("flow-dialog-error");
  if (errorEl) errorEl.textContent = "";
  syncFlowDraftFromDom();
  if (!(draft.name || "").trim()) {
    const message = "请填写流程名称。";
    if (errorEl) errorEl.textContent = message;
    setBanner(message, "bad");
    focusSoon("#flow-dialog [data-bind='flow-meta.name']");
    return;
  }
  const fields = (draft.fields || []).filter((item) => (item.key || "").trim());
  if (!draft.id) {
    const created = await api("POST", "/api/flows", {
      name: draft.name.trim(),
      description: draft.description || "",
      group: draft.group || "",
      tags: draft.tags || [],
      status: draft.status || "启用",
      fields,
    });
    S.flows.unshift(created);
  } else {
    const current = S.flows.find((item) => item.id === draft.id) || {};
    const saved = await api("PUT", "/api/flows/" + encodeURIComponent(draft.id), {
      name: draft.name.trim(),
      description: draft.description || "",
      group: draft.group || "",
      tags: draft.tags || [],
      status: draft.status || "启用",
      fields,
      graph: current.graph,
      schedule: current.schedule,
    });
    const index = S.flows.findIndex((item) => item.id === saved.id);
    if (index >= 0) S.flows[index] = saved;
    if (S.flow?.id === saved.id) S.flow = saved;
  }
  closeFlowMeta();
  renderFlowToolbar();
  renderFlowList();
  setBanner("已保存。", "ok");
}

async function deleteFlow(id) {
  const flow = S.flows.find((item) => item.id === (id || S.flow?.id));
  if (!flow || !await confirmAction("删除流程「" + flow.name + "」？元素会留下。")) return;
  await api("DELETE", "/api/flows/" + encodeURIComponent(flow.id));
  S.flows = S.flows.filter((item) => item.id !== flow.id);
  S.flowChecked = (S.flowChecked || []).filter((item) => item !== flow.id);
  if (S.flow?.id === flow.id) {
    S.flow = S.flows[0] || null;
    S.editing = false;
    S.selected = null;
  }
  render();
}

async function deleteCheckedFlows() {
  const ids = [...(S.flowChecked || [])];
  if (!ids.length) {
    setBanner("先勾选要删除的流程。", "bad");
    return;
  }
  if (!await confirmAction("删除勾选的 " + ids.length + " 条流程？元素会留下。")) return;
  for (const id of ids) await api("DELETE", "/api/flows/" + encodeURIComponent(id));
  S.flows = S.flows.filter((item) => !ids.includes(item.id));
  if (S.flow && ids.includes(S.flow.id)) {
    S.flow = S.flows[0] || null;
    S.editing = false;
  }
  S.flowChecked = [];
  render();
}

async function exportFlows(ids) {
  const list = ids ? [...ids] : [];
  if (ids && !list.length) {
    setBanner("先勾选要导出的流程。", "bad");
    return;
  }
  const query = list.length ? "?ids=" + encodeURIComponent(list.join(",")) : "";
  const pkg = await api("GET", "/api/flows/batch-package" + query);
  download(list.length ? "流程勾选.json" : "流程全部.json", pkg);
}

async function leaveEditor() {
  try { await persistNow(); } catch (error) { setBanner(error.message, "bad"); return; }
  S.editing = false;
  S.selected = null;
  S.linking = null;
  render();
}

async function selectFlow(id) {
  if (!id) return;
  try { await persistNow(); } catch (error) { setBanner(error.message, "bad"); return; }
  const flow = S.flows.find((item) => item.id === id);
  if (!flow) return;
  S.flow = flow;
  S.selected = null;
  S.linking = null;
  S.editing = true;
  S.tab = "flow";
  const runs = await api("GET", "/api/runs?flowId=" + encodeURIComponent(id) + "&limit=1");
  S.lastTrace = runs.runs[0]?.trace || null;
  try { await loadResults(); } catch (error) { setBanner(error.message, "bad"); }
  render();
}

async function runFlow() {
  if (S.running || !S.flow) return;
  const outputDir = document.getElementById("run-output-dir")?.value.trim() || "";
  const saveAs = document.getElementById("run-save-as")?.value || "";
  const outputNode = S.flow.graph.nodes.find((item) => item.type === "work.generate") || S.flow.graph.nodes.find((item) => item.type === "file.output");
  if (outputNode && (outputDir || saveAs)) {
    outputNode.params = outputNode.params || {};
    if (outputDir) outputNode.params.directory = outputDir;
    if (saveAs && outputNode.type === "file.output") outputNode.params.saveAs = saveAs;
  }
  const repeat = Math.max(1, Math.min(50, Number(document.getElementById("run-repeat")?.value) || 1));
  document.getElementById("run-dialog").classList.add("hidden");
  try { await persistNow(); } catch (error) { setBanner(error.message, "bad"); return; }
  S.running = true;
  renderTop();
  let finished = 0;
  try {
    let run = null;
    for (let index = 1; index <= repeat; index += 1) {
      setBanner(repeat === 1 ? "正在执行…" : `正在循环执行 ${index}/${repeat}…`, "");
      run = await api("POST", "/api/flows/" + encodeURIComponent(S.flow.id) + "/run", {
        themeText: document.getElementById("run-theme").value.trim(),
      });
      finished = index;
      S.lastTrace = run.trace;
      for (const node of Object.values(run.trace?.nodes || {})) {
        const references = node.export?.referencesUsed || [node.export?.referenceUsed];
        for (const used of references) {
          if (!used?.id) continue;
          const element = S.elements.find((item) => item.id === used.id);
          if (!element) continue;
          element.useCount = used.useCount;
          if (element.payload) element.payload.useCount = used.useCount;
        }
      }
      renderBoard();
      renderInspector();
      renderStageProducts();
      if (run.status !== "succeeded") {
        setBanner(`第 ${index}/${repeat} 次失败：${run.error || "执行失败"}`, "bad");
        return;
      }
    }
    const folder = run?.trace?.output?.folder || "";
    const zip = run?.trace?.output?.zip || "";
    const where = [folder, zip].filter(Boolean).join("  ");
    setBanner(repeat === 1 ? "执行完成。" + where : `循环完成 ${finished} 次。` + where, "ok");
  } catch (error) {
    setBanner(error.message, "bad");
  } finally {
    S.running = false;
    renderTop();
  }
}

async function pickOutputDirectory(target) {
  if (!window.desktop?.pickDirectory) {
    setBanner("当前窗口不能弹出文件夹选择。请直接填写路径。", "bad");
    return;
  }
  const chosen = await window.desktop.pickDirectory();
  if (!chosen) return;
  if (target === "run") {
    const input = document.getElementById("run-output-dir");
    if (input) input.value = chosen;
    return;
  }
  const node = findNode(S.selected?.id);
  if (!node || !["file.output", "result.direct", "work.generate"].includes(node.type)) return;
  node.params = node.params || {};
  node.params.directory = chosen;
  touch();
  renderInspector();
}

async function exportFlow() {
  await persistNow();
  const pkg = await api("GET", "/api/flows/" + encodeURIComponent(S.flow.id) + "/package");
  download((S.flow.name || "flow") + ".json", pkg);
}

async function exportLibrary() {
  await flushElement();
  const pkg = await api("GET", "/api/library/package");
  download("元素库.json", pkg);
}

function download(name, payload) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}

async function importPackage(file) {
  try { await persistNow(); } catch (error) { setBanner(error.message, "bad"); }
  let pkg;
  try {
    pkg = JSON.parse(await file.text());
  } catch (error) {
    setBanner("这个文件不是 JSON。", "bad");
    return;
  }
  const result = await api("POST", "/api/packages", pkg);
  S.editing = Boolean(result.flowId);
  await load(result.flowId || S.flow?.id);
  setBanner(result.flowId ? "已导入流程和它的元素。" : "已导入元素。", "ok");
}

function openNewElement() {
  const type = S.elementTypeFilter || "prompt";
  openElementEditor({
    id: "",
    type,
    name: "",
    group: S.elementGroupFilter || "",
    tags: S.elementTagFilter ? [S.elementTagFilter] : [],
    fields: [],
    payload: clone(S.elementDefaults[type] || {}),
  });
}

function syncElementDraftFromDom() {
  const root = document.getElementById("element-dialog-body");
  const draft = S.elementDraft;
  if (!root || !draft) return;
  const name = root.querySelector("[data-bind='element.name']");
  if (name) draft.name = name.value;
  const group = root.querySelector("[data-bind='element.group']");
  if (group) draft.group = group.value;
  const tags = root.querySelector("[data-bind='element.tags']");
  if (tags) draft.tags = tags.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
  const status = root.querySelector("[data-bind='element.status']");
  if (status) draft.status = status.value;
  const apiKeys = document.getElementById("api-keys");
  if (apiKeys) draft._apiKeys = apiKeys.value;
  root.querySelectorAll("[data-bind='custom-field']").forEach((el) => {
    const item = (draft.fields || [])[Number(el.dataset.index)];
    if (item) item[el.dataset.key] = el.value;
  });
  root.querySelectorAll("[data-bind^='payload.']").forEach((el) => {
    const key = el.dataset.bind.slice("payload.".length);
    if (el.type === "checkbox") draft.payload[key] = el.checked;
    else if (key === "tags") draft.payload[key] = el.value.split(/[,，]/).map((item) => item.trim()).filter(Boolean);
    else if (key === "temperature") draft.payload[key] = Number(el.value);
    else if (key === "maxTokens" || key === "concurrency") draft.payload[key] = el.value === "" ? null : Number(el.value);
    else draft.payload[key] = el.value;
  });
  root.querySelectorAll("[data-bind='variant'], [data-bind='step']").forEach((el) => {
    const bucket = el.dataset.bind === "variant" ? "variants" : "steps";
    const item = (draft.payload[bucket] || [])[Number(el.dataset.index)];
    if (!item) return;
    const key = el.dataset.key;
    item[key] = ["width", "height", "count"].includes(key) ? Number(el.value || 0) : el.value;
  });
}

function showElementError(message) {
  const errorEl = document.getElementById("element-dialog-error");
  if (errorEl) errorEl.textContent = message || "";
  if (message) setBanner(message, "bad");
}

async function uploadFile(file) {
  const response = await fetch("/api/uploads?name=" + encodeURIComponent(file.name), { method: "POST", body: file });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || "参考图上传失败");
  return data;
}

async function saveElementDraft() {
  const draft = S.elementDraft;
  if (!draft) return;
  showElementError("");
  try {
    syncElementDraftFromDom();
    const file = document.getElementById("ref-file")?.files?.[0];
    if (file) {
      const data = await uploadFile(file);
      draft.payload.path = data.path;
    }
    if (!(draft.name || "").trim()) {
      showElementError("请填写名称。");
      focusSoon("#element-dialog [data-bind='element.name']");
      return;
    }
    const apiKeys = (document.getElementById("api-keys")?.value || draft._apiKeys || "")
      .split(/\r?\n/)
      .map((item) => item.trim())
      .filter(Boolean);
    const body = {
      type: draft.type,
      name: draft.name.trim(),
      group: draft.group || "",
      tags: draft.tags || [],
      status: draft.status || "启用",
      fields: (draft.fields || []).filter((item) => (item.key || "").trim()),
      payload: draft.payload || {},
    };
    const saved = draft.id
      ? await api("PUT", "/api/elements/" + encodeURIComponent(draft.id), body)
      : await api("POST", "/api/elements", body);
    if (apiKeys.length) {
      const result = await api("POST", "/api/elements/" + encodeURIComponent(saved.id) + "/secret", { appendKeys: apiKeys });
      if (result.keyStatus?.hasKey) S.keyStatus[saved.id] = result.keyStatus;
      else delete S.keyStatus[saved.id];
    }
    const index = S.elements.findIndex((item) => item.id === saved.id);
    if (index >= 0) S.elements[index] = saved;
    else S.elements.push(saved);
    S.elementTypeFilter = saved.type;
    closeElementEditor();
    renderElementNav();
    renderElementToolbar();
    renderElementTable();
    setBanner("已保存。", "ok");
  } catch (error) {
    showElementError(error.message || "保存失败");
  }
}

async function deleteElement(id) {
  const element = S.elements.find((item) => item.id === id) || currentElement();
  if (!element || !await confirmAction("删除元素「" + element.name + "」？")) return;
  try {
    await api("DELETE", "/api/elements/" + encodeURIComponent(element.id));
  } catch (error) {
    if (!await confirmAction(error.message + "。仍然删除？")) return;
    await api("DELETE", "/api/elements/" + encodeURIComponent(element.id) + "?force=1");
  }
  S.elements = S.elements.filter((item) => item.id !== element.id);
  S.elementChecked = (S.elementChecked || []).filter((item) => item !== element.id);
  if (S.elementDraft?.id === element.id) closeElementEditor();
  renderElementToolbar();
  renderElementTable();
}

async function deleteChecked() {
  const ids = [...(S.elementChecked || [])];
  if (!ids.length) {
    setBanner("先勾选要删除的元素。", "bad");
    return;
  }
  if (!await confirmAction("删除勾选的 " + ids.length + " 个元素？")) return;
  const blocked = [];
  for (const id of ids) {
    try {
      await api("DELETE", "/api/elements/" + encodeURIComponent(id));
      S.elements = S.elements.filter((item) => item.id !== id);
    } catch (error) {
      blocked.push({ id, message: error.message });
    }
  }
  if (blocked.length && await confirmAction(blocked.length + " 个元素仍被流程使用。仍然删除？")) {
    for (const item of blocked) {
      await api("DELETE", "/api/elements/" + encodeURIComponent(item.id) + "?force=1");
      S.elements = S.elements.filter((element) => element.id !== item.id);
    }
  }
  S.elementChecked = [];
  renderElementToolbar();
  renderElementTable();
}

function exportChecked() {
  const chosen = S.elements.filter((item) => (S.elementChecked || []).includes(item.id));
  if (!chosen.length) {
    setBanner("先勾选要导出的元素。", "bad");
    return;
  }
  download("元素勾选.json", {
    format: "mp-content-flow-library",
    version: 1,
    elements: chosen.map((item) => ({
      id: item.id,
      type: item.type,
      name: item.name,
      group: item.group || "",
      tags: item.tags || [],
      fields: item.fields || [],
      payload: item.payload || {},
    })),
  });
}

function mutateElement(change) {
  const element = activeRecord();
  if (!element) return;
  if (S.elementDraft) syncElementDraftFromDom();
  change();
  if (S.elementDraft) renderElementDialog();
}

async function uploadImage() {
  const input = document.getElementById("ref-file");
  const file = input?.files?.[0];
  if (!file) {
    showElementError("先选一张图片。");
    return;
  }
  if (S.elementDraft) syncElementDraftFromDom();
  const data = await uploadFile(file);
  const element = activeRecord();
  element.payload.path = data.path;
  showElementError("");
  if (S.elementDraft) renderElementDialog();
  else await persistElement();
  setBanner("图片已上传，点保存后写入元素库。", "ok");
}

async function saveKey(clear) {
  const element = activeRecord();
  if (!element?.id) {
    setBanner("先保存配置，再改密钥。", "bad");
    return;
  }
  if (S.elementDraft) syncElementDraftFromDom();
  const result = await api("POST", "/api/elements/" + encodeURIComponent(element.id) + "/secret", { apiKeys: [] });
  if (result.keyStatus?.hasKey) S.keyStatus[element.id] = result.keyStatus;
  else delete S.keyStatus[element.id];
  if (S.elementDraft) {
    S.elementDraft._apiKeys = "";
    renderElementDialog();
  }
  setBanner(clear ? "已清除全部密钥。" : "密钥已更新。", "ok");
}

async function removeKey(index) {
  const element = activeRecord();
  if (!element?.id) {
    setBanner("先保存配置，再删除密钥。", "bad");
    return;
  }
  if (S.elementDraft) syncElementDraftFromDom();
  const result = await api("POST", "/api/elements/" + encodeURIComponent(element.id) + "/secret", { removeIndex: index });
  if (result.keyStatus?.hasKey) S.keyStatus[element.id] = result.keyStatus;
  else delete S.keyStatus[element.id];
  if (S.elementDraft) renderElementDialog();
  setBanner("已删除这条密钥。", "ok");
}

async function testModel() {
  const element = activeRecord();
  if (!element?.id) {
    setBanner("先保存元素，再测试接口。", "bad");
    return;
  }
  if (!await confirmAction("会向文本接口发送一次很短的请求，可能产生费用。")) return;
  try {
    const result = await api("POST", "/api/elements/" + encodeURIComponent(element.id) + "/test", {});
    setBanner("接口有回复：" + result.reply, "ok");
  } catch (error) {
    setBanner(error.message, "bad");
  }
}

async function openPath(encoded) {
  try {
    await api("POST", "/api/open", { path: decodeURIComponent(encoded) });
  } catch (error) {
    setBanner(error.message, "bad");
  }
}

function onMouseDown(event) {
  if (event.target.closest("input, textarea, select, .modal")) return;
  if (event.button !== 0 || !S.flow || !S.editing) return;
  const board = document.getElementById("board");
  const port = event.target.closest(".port");
  if (port && board) {
    const point = pointOnBoard(event, board);
    if (port.classList.contains("out")) {
      S.wireDrag = { fromId: port.dataset.id, edgeId: null, end: "to", x: point.x, y: point.y, sx: event.clientX, sy: event.clientY };
      event.preventDefault();
    }
    return;
  }
  const knob = event.target.closest("[data-edge-end]");
  if (knob && board) {
    const edge = findEdge(knob.dataset.id);
    if (!edge) return;
    const point = pointOnBoard(event, board);
    const fromId = knob.dataset.edgeEnd === "from" ? edge.to : edge.from;
    S.wireDrag = {
      fromId,
      edgeId: edge.id,
      end: knob.dataset.edgeEnd,
      x: point.x,
      y: point.y,
      sx: event.clientX,
      sy: event.clientY,
    };
    event.preventDefault();
    return;
  }
  const pal = event.target.closest(".pal");
  if (pal) {
    S.paletteDrag = { type: pal.dataset.type, sx: event.clientX, sy: event.clientY, moved: false };
    return;
  }
  if (event.target.closest("#board, #board-wrap, #wires") && !event.target.closest(".node, .port, .wire-handle")) {
    const wrap = document.getElementById("board-wrap");
    S.panning = { sx: event.clientX, sy: event.clientY, left: wrap.scrollLeft, top: wrap.scrollTop, moved: false };
    return;
  }
  const handle = event.target.closest("[data-drag]");
  if (!handle || event.target.closest(".port")) return;
  const card = handle.closest(".node");
  const node = findNode(card?.dataset.id);
  if (!node || !board) return;
  const origin = pointOnBoard(event, board);
  S.dragging = { id: node.id, dx: origin.x - (node.x || 0), dy: origin.y - (node.y || 0), sx: event.clientX, sy: event.clientY, moved: false };
  event.preventDefault();
}

function onMouseMove(event) {
  if (S.panning) {
    const wrap = document.getElementById("board-wrap");
    if (Math.hypot(event.clientX - S.panning.sx, event.clientY - S.panning.sy) > 4) S.panning.moved = true;
    wrap.scrollLeft = S.panning.left - (event.clientX - S.panning.sx);
    wrap.scrollTop = S.panning.top - (event.clientY - S.panning.sy);
    return;
  }
  const board = document.getElementById("board");
  if (S.wireDrag && board) {
    const point = pointOnBoard(event, board);
    S.wireDrag.x = point.x;
    S.wireDrag.y = point.y;
    drawWires();
    document.querySelectorAll(".port.drop").forEach((item) => item.classList.remove("drop"));
    const kind = S.wireDrag.end === "from" ? "out" : "in";
    nearestPort(event.clientX, event.clientY, kind)?.classList.add("drop");
    document.querySelectorAll(".node.drop").forEach((item) => item.classList.remove("drop"));
    const card = document.elementFromPoint(event.clientX, event.clientY)?.closest?.(".node");
    if (card && S.wireDrag.end === "to") card.classList.add("drop");
    return;
  }
  if (S.paletteDrag) {
    if (Math.hypot(event.clientX - S.paletteDrag.sx, event.clientY - S.paletteDrag.sy) > 6) {
      S.paletteDrag.moved = true;
      showGhost(event.clientX, event.clientY, typeInfo(S.paletteDrag.type).label);
    }
    return;
  }
  if (!S.dragging || !board) return;
  if (Math.hypot(event.clientX - S.dragging.sx, event.clientY - S.dragging.sy) > 4) S.dragging.moved = true;
  const node = findNode(S.dragging.id);
  if (!node) return;
  const origin = pointOnBoard(event, board);
  node.x = Math.max(8, origin.x - S.dragging.dx);
  node.y = Math.max(8, origin.y - S.dragging.dy);
  const card = document.querySelector(`.node[data-id="${CSS.escape(node.id)}"]`);
  if (card) {
    card.style.left = node.x + "px";
    card.style.top = node.y + "px";
  }
  drawWires();
}

function onMouseUp(event) {
  if (S.panning) {
    if (S.panning.moved) S.suppressClick = true;
    S.panning = null;
    return;
  }
  if (S.wireDrag) {
    finishWireDrag(event);
    return;
  }
  if (S.paletteDrag) {
    const drag = S.paletteDrag;
    S.paletteDrag = null;
    hideGhost();
    if (drag.moved) {
      S.suppressClick = true;
      const board = document.getElementById("board");
      if (!board) return;
      const rect = board.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (!inside) {
        setBanner("把节点拖到中间的画布上再松开。", "bad");
        return;
      }
      addNode(drag.type, pointOnBoard(event, board));
    }
    return;
  }
  if (!S.dragging) return;
  const moved = S.dragging.moved;
  S.dragging = null;
  if (moved) {
    S.suppressClick = true;
    touch();
  }
}

function finishWireDrag(event) {
  const drag = S.wireDrag;
  S.wireDrag = null;
  document.querySelectorAll(".port.drop, .node.drop").forEach((item) => item.classList.remove("drop"));
  const moved = Math.hypot(event.clientX - drag.sx, event.clientY - drag.sy) > 5;
  if (!moved) {
    drawWires();
    return;
  }
  S.suppressClick = true;
  const kind = drag.end === "from" ? "out" : "in";
  let port = nearestPort(event.clientX, event.clientY, kind);
  if (!port && drag.end === "to") {
    const card = document.elementFromPoint(event.clientX, event.clientY)?.closest?.(".node");
    if (card) port = { dataset: { id: card.dataset.id }, classList: { contains: () => false } };
  }
  if (!port) {
    setBanner(kind === "in" ? "拖到下一个节点左侧的入口再松开。" : "拖到一个节点右侧的出口再松开。", "bad");
    drawWires();
    return;
  }
  if (drag.edgeId) {
    const edge = findEdge(drag.edgeId);
    if (!edge) return;
    const nextFrom = drag.end === "from" ? port.dataset.id : edge.from;
    const nextTo = drag.end === "to" ? port.dataset.id : edge.to;
    if (nextFrom === nextTo || wouldCycle(nextFrom, nextTo, edge.id)) {
      setBanner("这样会形成环路。流程需要是有向无环图，下游不能再指回上游。", "bad");
      drawWires();
      return;
    }
    edge.from = nextFrom;
    edge.to = nextTo;
    if (edge.from === edge.to) {
      setBanner("节点不能连接自己。", "bad");
      drawWires();
      return;
    }
    S.selected = { kind: "edge", id: edge.id };
    renderBoard();
    renderInspector();
    touch();
    return;
  }
  connect(drag.fromId, port.dataset.id);
}

function nearestPort(clientX, clientY, kind) {
  const hit = document.elementFromPoint(clientX, clientY);
  const direct = hit?.closest?.(".port." + kind);
  if (direct) return direct;
  let best = null;
  let bestDist = 42;
  document.querySelectorAll(".port." + kind).forEach((port) => {
    const rect = port.getBoundingClientRect();
    const dist = Math.hypot(clientX - (rect.left + rect.width / 2), clientY - (rect.top + rect.height / 2));
    if (dist < bestDist) {
      bestDist = dist;
      best = port;
    }
  });
  return best;
}

function showGhost(x, y, label) {
  let ghost = document.getElementById("drag-ghost");
  if (!ghost) {
    ghost = document.createElement("div");
    ghost.id = "drag-ghost";
    document.body.appendChild(ghost);
  }
  ghost.textContent = label || "节点";
  ghost.style.left = x + 12 + "px";
  ghost.style.top = y + 12 + "px";
}

function hideGhost() {
  document.getElementById("drag-ghost")?.remove();
}

function pointOnBoard(event, board) {
  const rect = board.getBoundingClientRect();
  const zoom = S.zoom || 1;
  return { x: (event.clientX - rect.left) / zoom, y: (event.clientY - rect.top) / zoom };
}

function onKey(event) {
  const tag = event.target?.tagName || document.activeElement?.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (event.key !== "Delete") return;
  if (!S.selected || S.tab !== "flow") return;
  event.preventDefault();
  confirmDeleteSelection().catch((error) => setBanner(error.message, "bad"));
}

document.getElementById("run-go").addEventListener("click", () => runFlow().catch((error) => setBanner(error.message, "bad")));
document.querySelectorAll("[data-dismiss='run']").forEach((button) => {
  button.addEventListener("click", () => document.getElementById("run-dialog").classList.add("hidden"));
});
document.getElementById("run-dialog").addEventListener("click", (event) => {
  if (event.target.id === "run-dialog") event.currentTarget.classList.add("hidden");
});
document.getElementById("import-file").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (file) importPackage(file).catch((error) => setBanner(error.message, "bad"));
});
document.getElementById("table-file").addEventListener("change", (event) => {
  const file = event.target.files?.[0];
  event.target.value = "";
  if (file) importTable(file).catch((error) => setBanner(error.message, "bad"));
});
for (const id of ["element-dialog", "flow-dialog", "run-dialog", "conn-dialog", "result-dialog", "run-record-dialog", "reference-zip-dialog", "flow-copy-dialog", "batch-run-dialog", "confirm-dialog"]) {
  const dialog = document.getElementById(id);
  if (!dialog) continue;
  dialog.addEventListener("mousedown", (event) => event.stopPropagation());
  dialog.addEventListener("pointerdown", (event) => event.stopPropagation());
  dialog.addEventListener("keydown", (event) => event.stopPropagation());
}
document.body.addEventListener("click", (event) => onClick(event).catch((error) => setBanner(error.message, "bad")));
document.body.addEventListener("input", onField);
document.body.addEventListener("change", onField);
document.body.addEventListener("mousedown", onMouseDown);
window.addEventListener("mousemove", onMouseMove);
window.addEventListener("mouseup", onMouseUp);
document.addEventListener("keydown", onKey);
document.querySelector(".titlebar")?.addEventListener("dblclick", (event) => {
  if (event.target.closest(".window-buttons")) return;
  window.desktop?.window("maximize");
});
load().catch((error) => setBanner(error.message, "bad"));
setInterval(() => {
  if (!document.hidden && S.tab === "flow" && !S.editing) refreshBatchRuns().catch(() => {});
}, 2000);
document.getElementById("batch-run-dialog").addEventListener("click", (event) => {
  if (event.target.id === "batch-run-dialog") closeBatchRun();
});
document.getElementById("batch-run-dialog").addEventListener("keydown", (event) => {
  if (event.key === "Escape") { event.preventDefault(); closeBatchRun(); }
  if (event.key !== "Tab") return;
  const items = [...event.currentTarget.querySelectorAll('button, input, select')].filter((el) => !el.disabled && el.getClientRects().length);
  if (!items.length) return;
  if (event.shiftKey && document.activeElement === items[0]) { event.preventDefault(); items.at(-1).focus(); }
  else if (!event.shiftKey && document.activeElement === items.at(-1)) { event.preventDefault(); items[0].focus(); }
});
