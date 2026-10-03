(() => {
window.__aiServiceAppLoaded = true;
let bridge;
const status = document.getElementById("status");
const body = document.getElementById("accounts");
const dialog = document.getElementById("resume-dialog");
let selected = null;
let refreshing = false;
let refreshTimer;
let previewBusy = false;
const states = {running: "运行中", stopped: "已停止", pending: "连接中", error: "连接异常"};
const results = {sent: "已发送", reserved: "已预留 / 待核验", released: "已退回", uncertain: "发送结果不确定"};
const noticeStates = {pending: "待发送", sending: "发送中", sent: "已发送", blocked: "无法联系用户", uncertain: "待人工复核", cancelled: "已取消（条件变化）"};
const noticeKinds = {expired: "克隆服务到期", quota: "累计额度用完", daily: "历史账号日上限通知（已停用）"};
const date = value => new Date(value * 1000).toLocaleString();
async function wait(promise, ms, message) {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(message)), ms); }),
    ]);
  } finally { clearTimeout(timer); }
}
async function connect() {
  bridge = window.AstrBotPluginPage;
  if (!bridge) throw new Error("未检测到 AstrBot 插件桥接，请重新打开插件页面。");
  await wait(bridge.ready(), 8000, "插件桥接未建立，请检查父页面登录状态后重试；账号运行状态未知。");
}
const tabs = [...document.querySelectorAll('[role="tab"]')];
function activate(tab) {
  for (const item of tabs) {
    const active = item === tab;
    item.setAttribute("aria-selected", String(active));
    item.tabIndex = active ? 0 : -1;
    document.getElementById(`panel-${item.dataset.panel}`).hidden = !active;
  }
}
tabs.forEach((tab, index) => {
  tab.addEventListener("click", () => activate(tab));
  tab.addEventListener("keydown", event => {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 :
      (index + (event.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length;
    activate(tabs[next]); tabs[next].focus();
  });
});
activate(tabs[0]);
function empty(target, columns, text) {
  if (target.children.length) return;
  const row = document.createElement("tr");
  cell(row, text).colSpan = columns;
  target.append(row);
}

function cell(row, text, detail = "") {
  const td = document.createElement("td");
  td.textContent = text;
  if (detail) {
    const small = document.createElement("small");
    small.textContent = detail;
    td.append(small);
  }
  row.append(td);
  return td;
}

function userCell(row, tenant) {
  return cell(row, tenant.owner_username ? `@${tenant.owner_username}` : "用户名未设置或未获取",
    `Telegram ID：${tenant.owner}`);
}
function botCell(row, tenant) {
  return cell(row, tenant.bot_username ? `@${tenant.bot_username}` : "克隆管理 Bot",
    `Bot ID：${tenant.bot}`);
}
function accountLabel(account) {
  return account.username ? `@${account.username}` : account.account;
}
function accountList(target, accounts) {
  if (!accounts.length) { target.textContent = "未分配账号"; return; }
  for (const account of accounts) {
    const item = document.createElement("div");
    item.className = "account-identity";
    const title = document.createElement("span");
    title.textContent = accountLabel(account);
    const detail = document.createElement("small");
    detail.textContent = `平台别名：${account.account}`;
    item.append(title, detail);
    if (account.user_id) {
      const id = document.createElement("small");
      id.textContent = `账号 ID：${account.user_id}`;
      item.append(id);
    }
    target.append(item);
  }
}

function aiConfig(target, account) {
  const config = document.createElement("div");
  config.className = "ai-config";
  for (const [label, value, kind] of [
    ["配置档", account.profile_name || "配置档未获取", "profile"],
    ["对话模型", account.model || "未指定模型", "model"],
    ["人格 ID", account.persona || "未指定人格", "persona"],
  ]) {
    const field = document.createElement("div");
    field.className = `ai-field ai-${kind}`;
    const title = document.createElement("span");
    title.className = "ai-label";
    title.textContent = label;
    const content = document.createElement("span");
    content.className = "ai-value";
    content.textContent = value;
    field.append(title, content);
    config.append(field);
  }
  target.append(config);
}

function groupList(target, groups) {
  if (!groups.length) {
    const none = document.createElement("small");
    none.textContent = "未授权群";
    target.append(none);
    return;
  }
  const list = document.createElement("ul");
  list.className = "group-list";
  for (const group of groups) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.className = "group-name";
    name.textContent = group.name || "群名称未获取";
    const id = document.createElement("code");
    id.textContent = `群号：${group.id}`;
    item.append(name, id);
    if (group.name_state === "cached" || !group.name) {
      const note = document.createElement("small");
      note.textContent = group.name_state === "cached" ? "缓存名称 · 账号未连接" :
        group.name_state === "disconnected" ? "账号未连接" : "名称暂不可用";
      item.append(note);
    }
    list.append(item);
  }
  target.append(list);
}

function groupUsage(data) {
  const rows = [];
  const covered = new Set();
  for (const tenant of data.tenants) {
    if (tenant.expired) continue;
    const assigned = data.accounts.filter(a =>
      a.tenant === tenant.id && tenant.accounts.includes(a.account));
    for (const id of tenant.groups.map(String)) {
      const configured = assigned.filter(a => a.allowed_chats.map(String).includes(id));
      const available = configured.filter(a => !a.paused && a.enabled && a.state === "running");
      const disclosed = available.filter(a => a.disclosure_confirmed && a.allowed_senders.length);
      let state = "配置就绪";
      if (tenant.expired) state = "服务已到期";
      else if (!tenant.enabled) state = "用户服务已暂停";
      else if (tenant.remaining <= 0) state = "累计额度已用完";
      else if (!assigned.length) state = "未分配账号";
      else if (!configured.length) state = "群配置未完成";
      else if (configured.every(a => a.paused)) state = "账号已暂停";
      else if (!available.length) state = "账号未连接";
      else if (!disclosed.length) state = "成员授权未完成";
      const group = (tenant.group_details || []).find(g => String(g.id) === id)
        || configured.flatMap(a => a.groups || []).find(g => String(g.id) === id) || {id};
      rows.push({group, tenant, accounts: configured, state});
      covered.add(`${tenant.id}:${id}`);
    }
  }
  // Configured groups without a matching user grant must not look authorized.
  const extra = new Map();
  for (const account of data.accounts) {
    if (data.tenants.some(t => t.id === account.tenant && t.expired)) continue;
    for (const id of account.allowed_chats.map(String)) {
      const key = `${account.tenant || ""}:${id}`;
      if (covered.has(key)) continue;
      if (!extra.has(key)) {
        const tenant = data.tenants.find(t => t.id === account.tenant);
        extra.set(key, {
          group: (account.groups || []).find(g => String(g.id) === id) || {id},
          tenant, accounts: [], state: tenant ? "未获用户群授权" :
            account.tenant ? "授权记录未加载" : "未分配用户",
        });
      }
      extra.get(key).accounts.push(account);
    }
  }
  return rows.concat([...extra.values()]);
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  clearTimeout(refreshTimer);
  const button = document.getElementById("refresh");
  button.disabled = true;
  try {
    status.textContent = "正在读取运行状态…";
    await connect();
    const data = await wait(bridge.apiGet("status"), 12000, "状态接口无响应，请检查服务及页面授权后重试。");
    const overview = document.getElementById("overview");
    overview.replaceChildren();
    for (const [label, value] of [
      ["总控", data.control_attached ? "已连接" : "未连接"],
      ["账号连接", `${data.accounts.filter(a => a.state === "running").length} / ${data.accounts.length}`],
      ["未到期服务", String(data.tenant_counts?.current ?? data.tenants.filter(t => !t.expired).length)],
      ["客户入口", String(data.customer_entries)],
    ]) {
      const item = document.createElement("div");
      const title = document.createElement("span");
      title.textContent = label;
      const strong = document.createElement("strong");
      strong.textContent = value;
      item.append(title, strong); overview.append(item);
    }
    const selector = document.getElementById("account");
    const previous = selector.value;
    selector.replaceChildren();
    for (const account of data.accounts) {
      const option = document.createElement("option");
      option.value = account.account;
      option.textContent = `${accountLabel(account)} · ${account.account}`;
      selector.append(option);
    }
    if (data.accounts.some(a => a.account === previous)) selector.value = previous;
    document.getElementById("preview-submit").disabled = previewBusy || data.accounts.length === 0;
    body.replaceChildren();
    for (const account of data.accounts) {
      const row = document.createElement("tr");
      accountList(cell(row, ""), [account]);
      cell(row, account.paused ? "已紧急暂停" : (states[account.state] || account.state), account.enabled ? "连接配置已启用" : "连接配置未启用");
      aiConfig(cell(row, ""), account);
      const daily = cell(row, `${account.daily_used} 次`, "按用户授权限额 · 账号不设日上限");
      daily.classList.add("daily-usage");
      if (data.daily_window) {
        const reset = document.createElement("small");
        reset.className = "daily-reset";
        reset.textContent = "统计日切换：" + new Date(data.daily_window.reset_at * 1000).toLocaleString(
          "zh-CN", {timeZone: "Asia/Shanghai", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit"}
        ) + "（北京时间）";
        daily.append(reset);
      }
      const td = cell(row, "");
      const action = document.createElement("button");
      action.textContent = account.paused ? "解除暂停" : "紧急停用";
      action.addEventListener("click", async () => {
        if (account.paused) {
          selected = account.account;
          document.getElementById("resume-label").textContent = `账号：${selected}`;
          dialog.showModal();
          return;
        }
        action.disabled = true;
        try { await wait(bridge.apiPost("pause", {account: account.account}), 15000, "停用结果未确认，请刷新核验，不要重复操作。"); await refresh(); }
        catch (error) { status.textContent = error.message; }
        finally { action.disabled = false; }
      });
      td.append(action);
      body.append(row);
    }
    empty(body, 5, "暂无账号");
    const authorizations = document.getElementById("authorizations");
    authorizations.replaceChildren();
    const current = data.tenants.filter(t => !t.expired);
    const expired = data.tenants.filter(t => t.expired);
    document.getElementById("authorization-count").textContent = `显示 ${current.length} / ${data.tenant_counts?.current ?? current.length} 项未到期服务`;
    for (const tenant of current) {
      const row = document.createElement("tr");
      userCell(row, tenant);
      accountList(cell(row, ""), tenant.accounts.map(alias =>
        data.accounts.find(a => a.account === alias && a.tenant === tenant.id) || {account: alias}));
      botCell(row, tenant);
      cell(row, tenant.expired ? "已到期" : tenant.enabled ? "已启用" : "已暂停");
      cell(row, `${tenant.remaining} / ${tenant.budget}`, `已用 ${tenant.used}`);
      cell(row, date(tenant.expires));
      authorizations.append(row);
    }
    empty(authorizations, 6, "暂无未到期服务");
    const recycled = document.getElementById("recycled");
    recycled.replaceChildren();
    document.getElementById("recycle-count").textContent = `显示 ${expired.length} / ${data.tenant_counts?.expired ?? expired.length} 项到期服务`;
    for (const tenant of expired) {
      const row = document.createElement("tr");
      userCell(row, tenant);
      accountList(cell(row, ""), tenant.accounts.map(alias =>
        data.accounts.find(a => a.account === alias && a.tenant === tenant.id) || {account: alias}));
      botCell(row, tenant);
      cell(row, date(tenant.expires));
      groupList(cell(row, ""), tenant.group_details || tenant.groups.map(id => ({id})));
      cell(row, `${tenant.remaining} / ${tenant.budget}`, `已用 ${tenant.used}`);
      cell(row, noticeStates[tenant.expiry_notice || "pending"] || tenant.expiry_notice);
      recycled.append(row);
    }
    empty(recycled, 7, "暂无到期服务");
    const groups = document.getElementById("groups");
    groups.replaceChildren();
    const usage = groupUsage(data);
    document.getElementById("group-count").textContent = `${new Set(usage.map(r => String(r.group.id))).size} 个群 · ${usage.length} 项使用范围`;
    for (const entry of usage) {
      const row = document.createElement("tr");
      groupList(cell(row, ""), [entry.group]);
      if (entry.tenant) {
        userCell(row, entry.tenant);
        botCell(row, entry.tenant);
      } else {
        cell(row, entry.state === "授权记录未加载" ? "记录未加载" : "未分配用户");
        cell(row, "未关联管理 Bot");
      }
      accountList(cell(row, ""), entry.accounts);
      const members = cell(row, "");
      for (const account of entry.accounts) {
        const detail = document.createElement("small");
        detail.textContent = `${accountLabel(account)}：${account.allowed_senders.length} 位成员 · ${account.disclosure_confirmed ? "身份告知已确认" : "身份告知未确认"}`;
        members.append(detail);
      }
      if (!entry.accounts.length) members.textContent = "未配置";
      cell(row, entry.state);
      groups.append(row);
    }
    empty(groups, 6, "暂无群聊使用范围");
    const management = document.getElementById("management-groups");
    management.replaceChildren();
    const managed = data.group_management?.bindings || [];
    document.getElementById("management-count").textContent =
      `${managed.length} 项绑定 · ${data.group_management?.uncertain || 0} 项操作待复核`;
    for (const binding of managed) {
      const row = document.createElement("tr");
      cell(row, binding.title, `群号：${binding.chat}`);
      const owner = (data.tenants || []).find(tenant => tenant.owner === binding.owner);
      const identity = owner?.owner_username ? `@${owner.owner_username}` : `Telegram ID：${binding.owner}`;
      cell(row, binding.public ? "公共群管" : identity,
        binding.public ? `首次授权管理员：${identity}` : "克隆所有者");
      cell(row, binding.platform, `Bot ID：${binding.bot}`);
      cell(row, binding.automatic || "未指定");
      cell(row, `欢迎：${binding.welcome_enabled ? "开启" : "关闭"}`,
        `关键词：${binding.keywords_enabled ? "开启" : "关闭"}`);
      cell(row, binding.expired ? "已到期" : binding.disabled_reason || "有效",
        binding.automatic === binding.platform ? "自动执行入口" : "仅定向命令");
      management.append(row);
    }
    empty(management, 6, "暂无群管绑定");
    const checks = document.getElementById("checks");
    checks.replaceChildren();
    for (const [key, label] of Object.entries({
      telegram_hooks: "Telegram 服务处理器接口", hot_profile_pipeline: "账号原生 AI 流水线",
      controller_bound: "总控处理器绑定",
    })) {
      const term = document.createElement("dt");
      const value = document.createElement("dd");
      term.textContent = label;
      value.textContent = data.checks[key] ? "已检测到" : "未检测到 · 需核验";
      value.className = data.checks[key] ? "ok" : "warning";
      checks.append(term, value);
    }
    const portable = data.compatibility?.controller_transport === "telethon_ai_service";
    document.getElementById("compatibility-status").textContent = portable
      ? "插件自有 Telegram 接入。原版 v4.28.1 隔离启动与模拟更新验证已通过；真实 Telegram 开通、支付和模型服务仍需单独验收。角色化原生配置页是可选底座扩展。"
      : "当前总控仍使用旧 Telegram 适配器，依赖本机扩展。新版提供独立接入方式，需经备份后迁移；不自动改动现有 Token 或平台。";
    const recent = document.getElementById("recent");
    recent.replaceChildren();
    for (const request of data.recent) {
      const row = document.createElement("tr");
      cell(row, date(request.created)); cell(row, request.account);
      cell(row, request.tenant); cell(row, results[request.state] || request.state);
      recent.append(row);
    }
    empty(recent, 4, "暂无请求");
    const notices = document.getElementById("notices");
    notices.replaceChildren();
    for (const notice of data.notifications || []) {
      const row = document.createElement("tr");
      const tenant = data.tenants.find(t => t.owner === notice.owner && t.bot === notice.bot);
      cell(row, date(notice.delivered || notice.created));
      if (tenant) {
        userCell(row, tenant);
        botCell(row, tenant);
      } else {
        cell(row, `Telegram ID：${notice.owner}`);
        cell(row, `Bot ID：${notice.bot}`);
      }
      cell(row, noticeKinds[notice.kind] || notice.kind, notice.account);
      cell(row, noticeStates[notice.state] || notice.state);
      notices.append(row);
    }
    empty(notices, 5, "暂无通知");
    for (const table of document.querySelectorAll("table")) {
      const labels = [...table.querySelectorAll("th")].map(th => th.textContent);
      for (const row of table.querySelectorAll("tbody tr")) {
        [...row.children].forEach((td, index) => {
          td.dataset.label = row.children.length > 1 ? labels[index] : "";
        });
      }
    }
    status.textContent = `已更新 · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    status.textContent = `刷新失败，当前数据可能已过期：${error.message}`;
  }
  finally {
    button.disabled = false;
    refreshing = false;
    scheduleRefresh();
  }
}
dialog.addEventListener("close", async () => {
  if (dialog.returnValue !== "confirm" || !selected) return;
  try { await wait(bridge.apiPost("resume", {account: selected, confirm: true}), 15000, "解除暂停结果未确认，请刷新核验。"); await refresh(); }
  catch (error) { status.textContent = error.message; }
  finally { selected = null; }
});
document.getElementById("refresh").addEventListener("click", refresh);
const addDialog = document.getElementById("add-dialog");
const addForm = document.getElementById("add-form");
let ticket = null;
let loginState = "start";
let loginBusy = false;
function loginStep(state) {
  loginState = state;
  document.getElementById("login-start").hidden = state !== "start";
  for (const input of document.querySelectorAll("#login-start input")) input.disabled = state !== "start";
  for (const field of ["code", "password"]) {
    const input = document.getElementById(`login-${field}`);
    document.getElementById(`login-${field}-field`).hidden = state !== field;
    input.required = state === field;
    input.disabled = state !== field;
  }
  document.getElementById("login-submit").textContent = state === "start" ? "发送验证码" : "确认授权";
}
document.getElementById("add-account").addEventListener("click", () => {
  if (!ticket) { addForm.reset(); loginStep("start"); document.getElementById("login-status").textContent = ""; }
  addDialog.showModal();
});
async function cancelLogin() {
  if (loginBusy) return;
  loginBusy = true;
  try {
    if (ticket) await wait(bridge.apiPost("account-confirm", {ticket, cancel: true}), 15000, "取消未确认，登录申请将在五分钟后过期。");
    ticket = null; addForm.reset(); addDialog.close();
  } catch (error) { document.getElementById("login-status").textContent = error.message; }
  finally { loginBusy = false; }
}
document.getElementById("login-cancel").addEventListener("click", cancelLogin);
addDialog.addEventListener("cancel", event => { event.preventDefault(); cancelLogin(); });
addForm.addEventListener("submit", async event => {
  event.preventDefault();
  if (loginBusy) return;
  loginBusy = true;
  const submit = document.getElementById("login-submit");
  submit.disabled = true;
  document.getElementById("login-status").textContent = "正在验证…";
  try {
    await connect();
    let result;
    if (loginState === "start") {
      result = await wait(bridge.apiPost("account-login", {
        alias: document.getElementById("login-alias").value.trim(),
        phone: document.getElementById("login-phone").value.trim(),
        api_id: Number(document.getElementById("login-api-id").value),
        api_hash: document.getElementById("login-api-hash").value.trim(),
        consent: document.getElementById("login-consent").checked,
      }), 40000, "申请结果未知，请等待五分钟后重新申请。");
      ticket = result.ticket;
      document.getElementById("login-api-hash").value = "";
    } else {
      const payload = {ticket};
      payload[loginState] = document.getElementById(`login-${loginState}`).value;
      result = await wait(bridge.apiPost("account-confirm", payload), 40000, "授权结果未知，请刷新账号列表核验。");
    }
    if (result.state === "registered") {
      ticket = null; addForm.reset(); addDialog.close();
      await refresh();
      status.textContent = result.profile_pending ? "账号已登记，配置待同步；请检查模型与路由后同步登记。" : "账号已添加，连接保持停用。";
    } else {
      loginStep(result.state);
      document.getElementById("login-status").textContent = result.state === "password" ? "此账号需要两步验证。" : "验证码已发送，申请有效期五分钟。";
    }
  } catch (error) { document.getElementById("login-status").textContent = error.message; }
  finally {
    document.getElementById("login-code").value = "";
    document.getElementById("login-password").value = "";
    loginBusy = false; submit.disabled = false;
  }
});
document.getElementById("sync-accounts").addEventListener("click", async event => {
  event.target.disabled = true;
  try {
    await connect();
    await wait(bridge.apiPost("account-sync", {}), 20000, "同步结果未确认，请刷新核验。");
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { event.target.disabled = false; }
});
document.getElementById("preview-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = document.getElementById("preview-submit");
  const reply = document.getElementById("reply");
  button.disabled = true;
  previewBusy = true;
  reply.textContent = "生成中…";
  try {
    await connect();
    const result = await wait(bridge.apiPost("preview", {
      account: document.getElementById("account").value,
      prompt: document.getElementById("prompt").value,
    }), 55000, "预览结果超时；请求可能已执行，请勿连续重试。");
    reply.textContent = result.reply;
  } catch (error) { reply.textContent = error.message; }
  finally { previewBusy = false; button.disabled = false; }
});
function canAutoRefresh() {
  return document.visibilityState === "visible" && !dialog.open && !addDialog.open && !loginBusy && !previewBusy;
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => {
    if (canAutoRefresh()) refresh();
    else scheduleRefresh();
  }, 30000);
}
document.addEventListener("visibilitychange", () => {
  if (canAutoRefresh()) refresh();
});
refresh();
})();
