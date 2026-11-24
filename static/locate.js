/* 故障依赖定位演练台前端（原生 JS，无依赖）。
 * 状态以服务端 GET /api/locate/drills/<id> 为准，每秒轮询，
 * 因此两个页面打开同一 ?drill=id 时能看到同一轮 sending/done。
 */
"use strict";

const MAX_SERVICES = 8;
const MAX_PROBES = 8;

const ui = {
  setupView: document.getElementById("setup-view"),
  drillView: document.getElementById("drill-view"),
  servicesEditor: document.getElementById("services-editor"),
  faultPicker: document.getElementById("fault-picker"),
  probesEditor: document.getElementById("probes-editor"),
  addService: document.getElementById("add-service"),
  addProbe: document.getElementById("add-probe"),
  loadExample: document.getElementById("load-example"),
  loadTreeExample: document.getElementById("load-tree-example"),
  createDrill: document.getElementById("create-drill"),
  createError: document.getElementById("create-error"),
  budgetInput: document.getElementById("budget-input"),
  roundcapInput: document.getElementById("roundcap-input"),
  nameInput: document.getElementById("name-input"),
  healthPill: document.getElementById("health-pill"),
  toastStack: document.getElementById("toast-stack"),
};

const setup = {
  services: ["gateway", "auth", "order", "pay"],
  fault: "pay",
  // 每条草稿：{name, route, selected:Set/array}
  probes: [
    { name: "探针1", route: "/health", services: ["gateway"] },
    { name: "探针2", route: "/api/login", services: ["auth"] },
    { name: "探针3", route: "/api/checkout", services: ["order", "pay"] },
    { name: "探针4", route: "/api/inventory", services: ["order"] },
  ],
  routes: [],
};

const drillUi = {
  id: null,
  state: null,
  timer: null,
  sending: false,
  autoFollow: true,
  selected: new Set(),
};

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const resp = await fetch(url, opt);
  let data = null;
  try { data = await resp.json(); } catch { /* 非 JSON */ }
  if (!resp.ok) {
    const err = new Error((data && data.error) || `HTTP ${resp.status}`);
    err.status = resp.status;
    err.code = data && data.error_code;
    err.data = data || {};
    throw err;
  }
  return data;
}

function toast(message, kind = "info", timeout = 4200) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  ui.toastStack.appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .3s";
    setTimeout(() => el.remove(), 320);
  }, timeout);
}

function fmtTime(ts) {
  if (!ts) return "–";
  const d = new Date(ts * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// ---------------------------------------------------------------------------
// 健康检查 + 路由选项
// ---------------------------------------------------------------------------

async function checkHealth() {
  try {
    await api("GET", "/api/health");
    ui.healthPill.textContent = "服务在线";
    ui.healthPill.className = "pill ok";
  } catch {
    ui.healthPill.textContent = "服务不可达";
    ui.healthPill.className = "pill bad";
  }
}

async function loadRouteOptions() {
  try {
    const data = await api("GET", "/api/locate/routes");
    setup.routes = data.routes || [];
  } catch (e) {
    setup.routes = [];
    toast("路由列表加载失败：" + e.message, "error");
  }
}

// ---------------------------------------------------------------------------
// 录入页渲染
// ---------------------------------------------------------------------------

function renderServicesEditor() {
  ui.servicesEditor.innerHTML = setup.services.map((name, i) => `
    <div class="service-row">
      <span class="idx">${i + 1}.</span>
      <input data-svc-idx="${i}" maxlength="24" value="${esc(name)}"
             placeholder="服务名，如 pay">
      <button type="button" class="btn small icon-btn danger" data-del-svc="${i}"
              ${setup.services.length <= 1 ? "disabled" : ""}>✕</button>
    </div>`).join("");

  ui.servicesEditor.querySelectorAll("input[data-svc-idx]").forEach((inp) => {
    inp.addEventListener("input", () => {
      const i = Number(inp.dataset.svcIdx);
      const old = setup.services[i];
      setup.services[i] = inp.value.trim();
      // 同步探针勾选里的改名
      setup.probes.forEach((p) => {
        const k = p.services.indexOf(old);
        if (k >= 0 && old) p.services[k] = inp.value.trim();
      });
      renderFaultPicker();
      renderProbesEditor();
    });
  });
  ui.servicesEditor.querySelectorAll("[data-del-svc]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const i = Number(btn.dataset.delSvc);
      const removed = setup.services[i];
      setup.services.splice(i, 1);
      setup.probes.forEach((p) => {
        p.services = p.services.filter((s) => s !== removed);
      });
      if (setup.fault === removed) setup.fault = setup.services[0] || "";
      renderAllSetup();
    });
  });

  ui.addService.disabled = setup.services.length >= MAX_SERVICES;
  ui.addService.textContent = setup.services.length >= MAX_SERVICES
    ? `最多 ${MAX_SERVICES} 个服务` : "＋ 添加服务";
}

function renderFaultPicker() {
  if (!setup.services.length) {
    ui.faultPicker.innerHTML = `<span class="hint">先录入服务</span>`;
    return;
  }
  if (!setup.services.includes(setup.fault)) setup.fault = setup.services[0];
  ui.faultPicker.innerHTML = setup.services.map((name) => `
    <span class="fault-chip ${name === setup.fault ? "active" : ""}" data-fault="${esc(name)}">
      ⚡ ${esc(name)}
    </span>`).join("");
  ui.faultPicker.querySelectorAll("[data-fault]").forEach((chip) => {
    chip.addEventListener("click", () => {
      setup.fault = chip.dataset.fault;
      renderFaultPicker();
    });
  });
}

function routeOptionsHtml(selected) {
  if (!setup.routes.length) {
    return `<option value="">（没有现有路由）</option>`;
  }
  return setup.routes.map((r) =>
    `<option value="${esc(r.path)}" ${r.path === selected ? "selected" : ""}>
       ${esc(r.path)}${r.enabled ? "" : "（维护停用中）"}${r.note ? " · " + esc(r.note) : ""}
     </option>`).join("");
}

function renderProbesEditor() {
  if (!setup.probes.length) {
    ui.probesEditor.innerHTML = `<p class="probe-empty-svc">还没有探针，点下方按钮添加（最多 ${MAX_PROBES} 条）。</p>`;
  } else {
    ui.probesEditor.innerHTML = setup.probes.map((p, i) => `
      <div class="probe-edit" data-probe-idx="${i}">
        <div class="probe-edit-head">
          <span class="p-idx">#${i + 1}</span>
          <input class="name" data-p-name="${i}" maxlength="24" value="${esc(p.name)}" placeholder="探针名">
          <select data-p-route="${i}">${routeOptionsHtml(p.route)}</select>
          <button type="button" class="btn small icon-btn danger" data-del-probe="${i}"
                  ${setup.probes.length <= 0 ? "" : ""}>删除</button>
        </div>
        <div class="probe-services">
          ${setup.services.length === 0 ? `<span class="probe-empty-svc">先在左侧添加服务</span>` :
            setup.services.map((s) => `
              <label class="svc-check ${p.services.includes(s) ? "checked" : ""}">
                <input type="checkbox" data-p-svc="${i}" value="${esc(s)}"
                       ${p.services.includes(s) ? "checked" : ""}>
                ${esc(s)}
              </label>`).join("")}
        </div>
      </div>`).join("");
  }

  ui.probesEditor.querySelectorAll("[data-p-name]").forEach((inp) => {
    inp.addEventListener("input", () => { setup.probes[Number(inp.dataset.pName)].name = inp.value; });
  });
  ui.probesEditor.querySelectorAll("[data-p-route]").forEach((sel) => {
    sel.addEventListener("change", () => { setup.probes[Number(sel.dataset.pRoute)].route = sel.value; });
  });
  ui.probesEditor.querySelectorAll("[data-p-svc]").forEach((cb) => {
    cb.addEventListener("change", () => {
      const i = Number(cb.dataset.pSvc);
      const v = cb.value;
      const arr = setup.probes[i].services;
      const k = arr.indexOf(v);
      if (cb.checked && k < 0) arr.push(v);
      if (!cb.checked && k >= 0) arr.splice(k, 1);
      renderProbesEditor();
    });
  });
  ui.probesEditor.querySelectorAll("[data-del-probe]").forEach((btn) => {
    btn.addEventListener("click", () => {
      setup.probes.splice(Number(btn.dataset.delProbe), 1);
      renderProbesEditor();
    });
  });

  ui.addProbe.disabled = setup.probes.length >= MAX_PROBES;
  ui.addProbe.textContent = setup.probes.length >= MAX_PROBES
    ? `最多 ${MAX_PROBES} 条探针` : "＋ 添加探针";
}

function renderAllSetup() {
  renderServicesEditor();
  renderFaultPicker();
  renderProbesEditor();
}

ui.addService.addEventListener("click", () => {
  if (setup.services.length >= MAX_SERVICES) return;
  let base = `svc${setup.services.length + 1}`;
  let name = base, k = 2;
  while (setup.services.includes(name)) name = `${base}_${k++}`;
  setup.services.push(name);
  renderAllSetup();
});

ui.addProbe.addEventListener("click", () => {
  if (setup.probes.length >= MAX_PROBES) return;
  const firstRoute = setup.routes[0] ? setup.routes[0].path : "";
  setup.probes.push({ name: `探针${setup.probes.length + 1}`, route: firstRoute, services: [] });
  renderProbesEditor();
});

ui.loadExample.addEventListener("click", () => {
  setup.services = ["gateway", "auth", "order", "pay"];
  setup.fault = "pay";
  setup.probes = [
    { name: "p-gw", route: "/health", services: ["gateway"] },
    { name: "p-auth", route: "/api/login", services: ["auth"] },
    { name: "p-order", route: "/api/catalog", services: ["order"] },
    { name: "p-pay", route: "/api/checkout", services: ["pay"] },
  ];
  ui.budgetInput.value = 8;
  ui.roundcapInput.value = 3;
  renderAllSetup();
  toast("已填入 4 个单服务探针示例：一次并发 3 条即可定位（第 4 个靠排除）", "success");
});

ui.loadTreeExample.addEventListener("click", () => {
  // 5 服务 + 4 条组探针：存在最坏 3 次 / 3 轮的自适应策略，
  // 但不存在能一轮区分全部 5 个的固定批次，适合演示“安排下一轮”。
  setup.services = ["a", "b", "c", "d", "e"];
  setup.fault = "d";
  setup.probes = [
    { name: "t1-ab", route: "/api/login", services: ["a", "b"] },
    { name: "t2-a", route: "/health", services: ["a"] },
    { name: "t3-cd", route: "/api/catalog", services: ["c", "d"] },
    { name: "t4-c", route: "/api/checkout", services: ["c"] },
  ];
  ui.budgetInput.value = 5;
  ui.roundcapInput.value = 3;
  renderAllSetup();
  toast("已填入自适应多轮示例：推荐先发送 t1-ab", "success");
});

// ---------------------------------------------------------------------------
// 创建演练
// ---------------------------------------------------------------------------

ui.createDrill.addEventListener("click", async () => {
  ui.createError.textContent = "";
  const payload = {
    name: ui.nameInput.value.trim(),
    services: setup.services.map((s) => s.trim()).filter(Boolean),
    fault_service: setup.fault,
    total_budget: Number(ui.budgetInput.value),
    round_cap: Number(ui.roundcapInput.value),
    probes: setup.probes.map((p) => ({
      name: p.name, route: p.route, services: p.services,
    })),
  };
  if (!payload.services.length) {
    ui.createError.textContent = "至少录入 1 个服务";
    return;
  }
  if (!payload.fault_service) {
    ui.createError.textContent = "请选定故障服务";
    return;
  }
  if (!payload.total_budget || payload.total_budget < 1) {
    ui.createError.textContent = "试发总次数至少为 1";
    return;
  }
  if (!payload.round_cap || payload.round_cap < 1) {
    ui.createError.textContent = "轮数上限至少为 1";
    return;
  }
  ui.createDrill.disabled = true;
  try {
    const data = await api("POST", "/api/locate/drills", payload);
    const id = data.drill.id;
    history.replaceState(null, "", `/?drill=${id}`);
    openDrill(id);
    toast("演练场已创建，故障服务本场固定", "success");
  } catch (e) {
    ui.createError.textContent = e.message;
  } finally {
    ui.createDrill.disabled = false;
  }
});

// ---------------------------------------------------------------------------
// 演练视图 + 轮询
// ---------------------------------------------------------------------------

function openDrill(id) {
  drillUi.id = id;
  ui.setupView.hidden = true;
  ui.drillView.hidden = false;
  if (drillUi.timer) clearInterval(drillUi.timer);
  refreshDrill(true);
  drillUi.timer = setInterval(() => refreshDrill(false), 1000);
}

function closeDrill() {
  if (drillUi.timer) clearInterval(drillUi.timer);
  drillUi.timer = null;
  drillUi.id = null;
  drillUi.state = null;
  ui.drillView.hidden = true;
  ui.setupView.hidden = false;
  history.replaceState(null, "", "/");
}

document.getElementById("back-setup").addEventListener("click", closeDrill);

document.getElementById("copy-link").addEventListener("click", async () => {
  const url = `${location.origin}/?drill=${drillUi.id}`;
  try {
    await navigator.clipboard.writeText(url);
    toast("已复制：在第二个页面打开同一链接即可验证同轮去重", "success", 6000);
  } catch {
    window.prompt("复制此链接到第二个页面：", url);
  }
});

document.getElementById("refresh-now").addEventListener("click", () => refreshDrill(true));

function recommendedIds(d) {
  return new Set(((d.verdict && d.verdict.batch) || []).map((p) => p.id));
}

async function refreshDrill(manual) {
  if (!drillUi.id) return;
  try {
    const data = await api("GET", `/api/locate/drills/${drillUi.id}`);
    const prevRound = drillUi.state && drillUi.state.rounds_quota.done;
    drillUi.state = data.drill;
    if (drillUi.autoFollow) {
      drillUi.selected = recommendedIds(data.drill);
    }
    renderDrill();
    // 另一页面刚把轮次跑完时给个轻提示
    if (!manual && prevRound != null && data.drill.rounds_quota.done > prevRound
        && !drillUi.sending) {
      toast(`另一页面完成了第 ${data.drill.rounds_quota.done} 轮，本页已同步`, "info");
    }
  } catch (e) {
    if (e.status === 404) {
      toast("演练不存在，返回录入页", "error");
      closeDrill();
    }
  }
}

// ---------------------------------------------------------------------------
// 渲染演练
// ---------------------------------------------------------------------------

function renderDrill() {
  const d = drillUi.state;
  if (!d) return;
  const cfg = d.config;
  document.getElementById("drill-name").textContent = cfg.name || "未命名演练";
  document.getElementById("drill-id").textContent = d.id;

  // 额度
  const b = d.budget;
  const be = document.getElementById("budget-remaining");
  be.textContent = b.remaining;
  document.getElementById("budget-detail").textContent = `已发 ${b.used} / 共 ${b.total}`;
  const bp = document.getElementById("qpill-budget");
  bp.classList.toggle("danger", b.remaining <= 0);
  bp.classList.toggle("warn", b.remaining > 0 && b.remaining <= 2);

  const rq = d.rounds_quota;
  document.getElementById("rounds-remaining").textContent = rq.remaining;
  document.getElementById("rounds-detail").textContent =
    `第 ${rq.done}/${cfg.round_cap} 轮已收齐` + (rq.in_flight ? " · 有一轮发送中" : "");
  const rp = document.getElementById("qpill-rounds");
  rp.classList.toggle("danger", rq.remaining <= 0);
  rp.classList.toggle("warn", rq.remaining === 1);

  document.getElementById("cand-count").textContent = d.candidates.length;

  renderVerdict(d);
  renderBatch(d);
  renderRoutes(d);
  renderCandidates(d);
  renderTrials(d);
  renderRounds(d);
  renderSendControls(d);
}

function renderVerdict(d) {
  const v = d.verdict;
  const card = document.getElementById("verdict-card");
  card.className = `card verdict-card ${v.kind}`;
  let html = "";
  if (v.kind === "identified") {
    const f = d.candidates[0];
    const match = d.fault_found;
    html = `
      <div class="verdict-title">🎯 只剩一个候选，故障服务定位为
        <span class="big-fault">⚡ ${esc(f)}</span>
        <span class="answer-check ${match ? "match" : ""}">
          ${match ? `与本场设定 ${esc(d.actual_fault_service)} 一致 ✓`
                  : `注意：与设定 ${esc(d.actual_fault_service)} 不一致`}
        </span>
      </div>
      <div class="verdict-detail">全部真实应答共同指向该依赖，演练结束。可点“新建演练”再来一场。</div>`;
  } else if (v.kind === "feasible") {
    html = `
      <div class="verdict-title">🧭 存在保证策略：发送右侧 / 下方推荐的下一批</div>
      <div class="verdict-detail">${esc(v.detail || "")}</div>
      <div class="verdict-stats">
        <span>本批 <b>${v.batch_indexes.length}</b> 条并发</span>
        <span>最坏共 <b>${v.worst_sends}</b> 次试发</span>
        <span>最坏 <b>${v.worst_rounds}</b> 轮查明</span>
        <span>当前候选 <b>${v.candidates.length}</b> 个</span>
      </div>`;
  } else if (v.kind === "infeasible") {
    const reasonMap = {
      no_strategy: "⚠️ 无可行策略",
      budget_exhausted: "⛔ 试发次数已用完",
      rounds_exhausted: "⛔ 轮数上限已用完",
      budget_insufficient: "⚠️ 剩余次数不足以保证查明",
    };
    html = `
      <div class="verdict-title">${reasonMap[v.reason] || "⚠️ 无可行策略"}</div>
      <div class="verdict-detail">${esc(v.detail || "")}</div>
      <div class="verdict-detail" style="margin-top:8px">
        尚未排除的服务（${v.candidates.length}）：
        ${v.candidates.map((s) => `<span class="svc-chip cand" style="margin:2px">${esc(s)}</span>`).join(" ")}
      </div>
      ${v.worst_sends != null ? `<div class="verdict-detail" style="margin-top:6px">参考：保证查明最坏需要 ${v.worst_sends} 次 / ${v.worst_rounds} 轮。</div>` : ""}`;
  } else {
    html = `
      <div class="verdict-title">❓ 候选集合异常</div>
      <div class="verdict-detail">${esc(v.detail || "")}</div>`;
  }
  card.innerHTML = html;
}

function renderBatch(d) {
  const v = d.verdict;
  const recSet = new Set(v.batch_indexes || []);
  const list = document.getElementById("batch-list");
  document.getElementById("batch-hint").textContent =
    v.kind === "feasible" ? `推荐第 ${d.rounds_quota.next_round_no} 轮批次` : "可手动勾选探针试发";

  list.innerHTML = d.config.probes.map((p, i) => {
    const route = d.routes.find((r) => r.path === p.route);
    const enabled = route ? route.enabled : false;
    const checked = drillUi.selected.has(p.id);
    return `
      <label class="batch-item ${recSet.has(i) ? "recommended" : ""} ${enabled ? "" : "route-off"}">
        <input type="checkbox" data-batch-probe="${esc(p.id)}" ${checked ? "checked" : ""}>
        <span class="b-name">${esc(p.name)}</span>
        <span class="b-route">${esc(p.route)}${enabled ? "" : "（停用→503）"}</span>
        <span class="b-path">经过：${p.services.length ? p.services.map(esc).join(" → ") : "（无模拟服务）"}</span>
        ${recSet.has(i) ? `<span class="b-tag">策略推荐</span>` : ""}
      </label>`;
  }).join("");

  list.querySelectorAll("[data-batch-probe]").forEach((cb) => {
    cb.addEventListener("change", () => {
      drillUi.autoFollow = false;
      document.getElementById("auto-follow").checked = false;
      const id = cb.dataset.batchProbe;
      if (cb.checked) drillUi.selected.add(id);
      else drillUi.selected.delete(id);
      renderSendControls(d);
    });
  });
}

document.getElementById("auto-follow").addEventListener("change", (e) => {
  drillUi.autoFollow = e.target.checked;
  if (drillUi.autoFollow && drillUi.state) {
    drillUi.selected = recommendedIds(drillUi.state);
    renderDrill();
  }
});

document.getElementById("pick-recommended").addEventListener("click", () => {
  if (!drillUi.state) return;
  drillUi.autoFollow = true;
  document.getElementById("auto-follow").checked = true;
  drillUi.selected = recommendedIds(drillUi.state);
  renderDrill();
  toast("已恢复为策略推荐批次", "info", 1800);
});

function renderSendControls(d) {
  const btn = document.getElementById("send-round");
  const err = document.getElementById("send-error");
  err.textContent = "";
  const rq = d.rounds_quota;
  const ids = Array.from(drillUi.selected);
  const recIds = (d.verdict.batch || []).map((p) => p.id);
  const sameAsRec = ids.length === recIds.length && ids.every((x, i) => x === recIds[i]);

  const inFlight = drillUi.sending || rq.in_flight > 0;
  const noRound = rq.remaining <= 0;
  const noBudget = d.budget.remaining <= 0;
  const tooBig = ids.length > d.budget.remaining;

  btn.disabled = inFlight || noRound || noBudget || ids.length === 0 || tooBig;

  if (drillUi.sending) {
    btn.innerHTML = `<span class="spinner"></span> 正在并发试发并收齐应答…`;
  } else if (rq.in_flight > 0) {
    btn.innerHTML = `<span class="spinner"></span> 另一页面正在发送同一轮…`;
  } else if (noRound) {
    btn.textContent = `轮数上限 ${d.config.round_cap} 已用完`;
  } else if (noBudget) {
    btn.textContent = "试发次数已用完";
  } else if (ids.length === 0) {
    btn.textContent = "请先勾选本轮探针";
  } else if (tooBig) {
    btn.textContent = `勾选 ${ids.length} 条 > 剩余 ${d.budget.remaining} 次`;
  } else {
    btn.textContent = `🚀 一键并发试发 · 第 ${rq.next_round_no} 轮 · ${ids.length} 条` +
      (sameAsRec ? "（策略推荐）" : "（手动批次）");
  }
}

document.getElementById("send-round").addEventListener("click", async () => {
  if (!drillUi.state || drillUi.sending) return;
  const d = drillUi.state;
  const ids = Array.from(drillUi.selected);
  if (!ids.length) return;
  const err = document.getElementById("send-error");
  err.textContent = "";
  drillUi.sending = true;
  renderSendControls(d);
  const roundNo = d.rounds_quota.next_round_no;
  try {
    const data = await api("POST", `/api/locate/drills/${drillUi.id}/rounds`, {
      round_no: roundNo,
      probe_ids: ids,
    });
    const sr = data.drill.send_result || {};
    if (sr.deduplicated) {
      toast(`两个页面同一轮只发一批：本请求已合并到第 ${sr.round_no} 轮，未重复试发`, "warn", 6000);
    } else {
      toast(`第 ${sr.round_no} 轮 ${sr.sent} 条探针已收齐应答`, "success");
    }
    drillUi.state = data.drill;
    if (drillUi.autoFollow) drillUi.selected = recommendedIds(data.drill);
    renderDrill();
  } catch (e) {
    if (e.code === "same_round_already_sent" || e.status === 409) {
      toast("同一轮已在另一页面发出 / 完成，正在同步结果", "warn");
      await refreshDrill(true);
    } else {
      err.textContent = e.message;
      toast("试发失败：" + e.message, "error", 6000);
    }
  } finally {
    drillUi.sending = false;
    if (drillUi.state) renderSendControls(drillUi.state);
  }
});

// ---------------------------------------------------------------------------
// 路由维护
// ---------------------------------------------------------------------------

function renderRoutes(d) {
  const box = document.getElementById("routes-list");
  box.innerHTML = d.routes.map((r) => `
    <div class="route-row">
      <label class="switch" title="启用 / 维护停用">
        <input type="checkbox" data-route="${esc(r.path)}" ${r.enabled ? "checked" : ""}>
        <span class="slider"></span>
      </label>
      <code>${esc(r.path)}</code>
      <span class="note">${esc(r.note || r.upstream || "")}</span>
      <span class="route-state ${r.enabled ? "on" : "off"}">${r.enabled ? "启用中 · 200/502" : "维护停用 · 503"}</span>
    </div>`).join("");
  box.querySelectorAll("input[data-route]").forEach((sw) => {
    sw.addEventListener("change", async () => {
      const path = sw.dataset.route;
      sw.disabled = true;
      try {
        await api("POST", "/api/locate/routes/maintenance", { path, enabled: sw.checked });
        toast(`${path} 已${sw.checked ? "恢复启用" : "因维护停用（网关将真实回 503）"}`,
          sw.checked ? "success" : "warn");
        await refreshDrill(true);
        await loadRouteOptions();
      } catch (e) {
        sw.checked = !sw.checked;
        toast("切换失败：" + e.message, "error");
      } finally {
        sw.disabled = false;
      }
    });
  });
}

// ---------------------------------------------------------------------------
// 候选面板
// ---------------------------------------------------------------------------

function renderCandidates(d) {
  const box = document.getElementById("candidates-panel");
  const only = d.candidates.length === 1;
  box.innerHTML = `
    <div class="chip-row">
      <span class="row-label">候选</span>
      ${d.candidates.length ? d.candidates.map((s) =>
        `<span class="svc-chip ${only ? "only" : "cand"}">${only ? "🎯 " : "▸ "}${esc(s)}</span>`).join("")
        : `<span class="form-error">（空）</span>`}
    </div>
    <div class="chip-row">
      <span class="row-label">已排除</span>
      ${d.excluded.length ? d.excluded.map((s) =>
        `<span class="svc-chip excluded">${esc(s)}</span>`).join("")
        : `<span class="hint">暂无（200 会排除探针路径上的全部服务，502 会交集到路径）</span>`}
    </div>
    <div class="chip-row">
      <span class="row-label">本场设定</span>
      <span class="hint">固定故障服务为 <b class="c-red">⚡ ${esc(d.actual_fault_service)}</b>
        （考官视角；策略规划不读取该字段，只依据真实应答）</span>
    </div>`;
}

// ---------------------------------------------------------------------------
// 应答表
// ---------------------------------------------------------------------------

function statusBadge(status) {
  const cls = status === 200 ? "status-200"
    : status === 502 ? "status-502"
    : status === 503 ? "status-503"
    : status === 0 ? "status-0" : "status-other";
  const text = status === 0 ? "ERR" : status;
  return `<span class="status-badge ${cls}">${text}</span>`;
}

function effectText(t) {
  const n = t.path_services.length;
  if (t.http_status === 200) {
    return `<span class="effect-clear">200 未经过故障 → 排除路径上 ${n} 个服务</span>`;
  }
  if (t.http_status === 502) {
    return `<span class="effect-hit">502 命中故障链路 → 候选交集到这 ${n} 个服务</span>`;
  }
  if (t.http_status === 503) {
    return `<span class="effect-none">503 维护停用 → 计次，不排除任何候选</span>`;
  }
  if (t.error) {
    return `<span class="effect-none">请求异常 → 不排除候选（${esc(t.error)}）</span>`;
  }
  return `<span class="effect-none">${t.http_status} → 无判定规则，不排除候选</span>`;
}

function renderTrials(d) {
  const tb = document.getElementById("trials-tbody");
  const empty = document.getElementById("trials-empty");
  const trials = d.trials || [];
  empty.style.display = trials.length ? "none" : "block";
  tb.innerHTML = trials.map((t) => `
    <tr>
      <td class="mini-svc" style="white-space:nowrap">${fmtTime(t.created_real)}</td>
      <td><span class="rno">#${t.round_no}</span></td>
      <td style="white-space:nowrap">${esc(t.probe_name)}<br><span class="mini-svc">${esc(t.probe_id)}</span></td>
      <td><code>${esc(t.route)}</code></td>
      <td>${t.path_services.map((s) => `<span class="mini-svc">${esc(s)}</span>`).join("<br>")}</td>
      <td>${statusBadge(t.http_status)}</td>
      <td>${effectText(t)}</td>
      <td class="mini-svc" style="white-space:nowrap">${t.elapsed_ms} ms</td>
    </tr>`).join("");
}

function renderRounds(d) {
  const box = document.getElementById("rounds-list");
  if (!d.rounds.length) {
    box.innerHTML = `<span class="hint">尚未安排轮次。</span>`;
    return;
  }
  box.innerHTML = d.rounds.map((r) => `
    <div class="round-row">
      <span class="rno">第 ${r.round_no} 轮</span>
      <span class="tag ${r.source}">${r.source === "strategy" ? "策略批次" : "手动批次"}</span>
      <span class="tag ${r.status}">${r.status === "done" ? `已收齐 ${r.trials.length} 条应答`
        : r.status === "sending" ? "发送中" : "中断"}</span>
      <span class="hint">${r.batch.map((p) => esc(p.name)).join("、")}</span>
      <span class="hint" style="margin-left:auto">${fmtTime(r.finished_real || r.started_real)}</span>
    </div>`).join("");
}

// ---------------------------------------------------------------------------
// 启动
// ---------------------------------------------------------------------------

(async function init() {
  await checkHealth();
  setInterval(checkHealth, 5000);
  await loadRouteOptions();
  // 建场表单里的路由下拉需要在 routes 到达后首渲
  renderAllSetup();

  const params = new URLSearchParams(location.search);
  const did = params.get("drill");
  if (did) {
    try {
      await api("GET", `/api/locate/drills/${did}`);
      openDrill(did);
    } catch {
      toast("链接中的演练不存在，显示新建表单", "warn");
    }
  }
})();
