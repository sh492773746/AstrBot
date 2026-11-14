(() => {
  const $ = id => document.getElementById(id);
  let bridge, allowed = false, busy = false, instances = [], customers = [], bindings = [];
  let rates = [], providers = [], page = 1;
  const pending = new Map();
  const errors = {
    commerce_operator_required: "仅指定运营管理员可以操作。",
    commerce_changed_refresh_required: "记录版本已更新，请刷新后核验。",
    commerce_receipt_already_used: "该收款记录已入账，请核验原订单。",
    commerce_idempotency_conflict: "操作记录冲突，请刷新核验。",
    commerce_instance_already_assigned: "此机器人已分配，不能跨客户转移。",
    commerce_disable_before_binding: "请先停用机器人，再分配给客户。",
    commerce_robot_limit: "套餐最多允许 1 个机器人。",
    commerce_group_limit: "套餐最多允许 10 个有效管理群。",
    commerce_subscription_inactive: "当前套餐未生效或已经到期。",
    commerce_midperiod_plan_change: "本轮不支持中途换套餐。",
    commerce_reconciliation_required: "存在待对账调用，请先完成对账。",
    commerce_use_renewal: "客户已有套餐记录，请使用续费操作。",
    commerce_ai_plan_required: "仅有效 AI 版可购买积分追加包。",
    commerce_binding_mismatch: "机器人账号绑定不匹配。",
    explicit_confirmation_required: "请勾选确认后再提交。",
    payment_confirmation_required: "请填写收款记录号并核实收款。",
    instance_not_selected: "请选择已经停用、尚未分配的机器人。",
  };
  const states = {
    active: "有效", paused: "暂停", reserved: "未发起预留", dispatched: "已发起",
    settled: "已结算", released: "未发起已释放", reconciled: "人工对账完成",
    unknown: "用量未知", overrun: "超出预留", pending: "待核验",
    ai_budget_low: "AI 积分不足", ai_exhausted: "AI 积分耗尽",
    ai_review_required: "AI 暂停，等待对账", service_inactive: "套餐到期或服务暂停",
  };
  const money = nano => (Number(nano || 0) / 1e9).toFixed(6);
  const points = micro => (Number(micro || 0) / 1e6).toLocaleString("zh-CN", {maximumFractionDigits: 3});
  const clock = stamp => new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit",
  }).format(new Date(stamp * 1000));
  const customer = () => customers.find(row => row.id === $("commerce-customer").value);
  function option(select, value, label) {
    const element = document.createElement("option");
    element.value = value; element.textContent = label; select.append(element);
  }
  function record(container, text) {
    const row = document.createElement("p"); row.textContent = text; container.append(row);
  }
  function fields() {
    const action = $("commerce-action").value;
    const paid = ["activate", "renew", "topup"].includes(action);
    $("commerce-plan-label").hidden = !["activate", "renew"].includes(action);
    $("commerce-topup-label").hidden = action !== "topup";
    $("commerce-bot-label").hidden = action !== "bind";
    $("commerce-receipt-label").hidden = $("commerce-paid-label").hidden = !paid;
    $("commerce-receipt").required = paid;
    $("commerce-confirm").checked = false;
  }
  async function action(fn) {
    if (!allowed || busy) return;
    busy = true; $("commerce").setAttribute("aria-busy", "true");
    const disabled = new Map([...$("commerce").querySelectorAll("button, input, select")]
      .map(control => [control, control.disabled]));
    disabled.forEach((value, control) => { control.disabled = true; });
    $("commerce-status").textContent = "";
    try { await fn(); }
    catch (error) {
      const code = error.error || error.message;
      $("commerce-status").textContent = errors[code] || "操作结果未确认，请刷新状态核验；未自动重试。";
    } finally {
      busy = false; $("commerce").removeAttribute("aria-busy");
      disabled.forEach((value, control) => { control.disabled = value; });
      $("commerce-previous").disabled = page <= 1;
    }
  }
  async function mutate(route, payload) {
    const key = JSON.stringify([route, payload]);
    const request_id = pending.get(key) || crypto.randomUUID();
    pending.set(key, request_id);
    const result = await bridge.apiPost(route, {...payload, request_id, confirm: true});
    pending.delete(key);
    return result;
  }
  async function usage() {
    const selected = customer();
    if (!selected) {
      for (const id of ["commerce-summary", "commerce-periods", "commerce-calls", "commerce-call", "commerce-alerts", "commerce-audit"]) $(id).replaceChildren();
      return;
    }
    $("commerce-customer-state").textContent = `${states[selected.state] || selected.state} · 版本 ${selected.version}${selected.ai_blocked ? " · AI 等待人工对账" : ""}`;
    const data = await bridge.apiPost("commerce-usage", {action: "status", customer_id: selected.id, page});
    $("commerce-page").textContent = String(page);
    $("commerce-summary").replaceChildren();
    const current = data.periods.find(period => period.active);
    for (const [label, value] of [
      ["套餐", current ? (current.plan === "ai" ? "AI 版" : "普通版") : "未生效"],
      ["剩余 AI 积分", current ? points(current.remaining_micro) : "0"],
      ["周期结束（北京时间）", current ? clock(current.end) : "—"],
    ]) {
      const cell = document.createElement("div"), title = document.createElement("span"), amount = document.createElement("strong");
      title.textContent = label; amount.textContent = value; cell.append(title, amount); $("commerce-summary").append(cell);
    }
    for (const id of ["commerce-periods", "commerce-calls", "commerce-call", "commerce-alerts", "commerce-audit"]) $(id).replaceChildren();
    for (const period of data.periods) record($("commerce-periods"),
      `${period.plan === "ai" ? "AI 版" : "普通版"} · ${clock(period.start)} 至 ${clock(period.end)} · 已用 ${points(period.used_micro)} / ${points(period.total_micro)} 积分 · 预留 ${points(period.held_micro)}`);
    for (const call of data.calls) {
      record($("commerce-calls"), `${clock(call.stamp)} · ${states[call.state] || call.state} · 预留 ¥${money(call.held_nano)} · 已结算 ¥${money(call.cost_nano)} · ${call.id}`);
      if (["unknown", "overrun"].includes(call.state)) option($("commerce-call"), call.id, `${clock(call.stamp)} · ${states[call.state]} · ${call.id.slice(0, 8)}`);
    }
    for (const alert of data.alerts) record($("commerce-alerts"), `${clock(alert.stamp)} · ${states[alert.kind] || alert.kind} · ${states[alert.state] || alert.state}`);
    for (const audit of data.audit) record($("commerce-audit"), `${clock(audit.stamp)} · ${audit.actor} · ${audit.kind}`);
    for (const event of data.call_events || []) record($("commerce-audit"), `${clock(event.stamp)} · 调用 ${event.call_id} · ${states[event.state] || event.state} · ${event.detail}`);
    $("commerce-previous").disabled = page <= 1;
  }
  async function refresh() {
    if (!allowed) return;
    const previous = $("commerce-customer").value;
    const data = await bridge.apiGet("commerce-customers");
    customers = data.customers; bindings = data.bindings;
    $("commerce-customer").replaceChildren();
    for (const row of customers) option($("commerce-customer"), row.id, row.label);
    if (customers.some(row => row.id === previous)) $("commerce-customer").value = previous;
    if (!customers.length) option($("commerce-customer"), "", "暂无客户");
    $("commerce-bindings").replaceChildren();
    for (const row of bindings) record($("commerce-bindings"), `${row.instance_id} · ${row.internal ? "内部实例（不计商业账单）" : customers.find(c => c.id === row.customer_id)?.label || row.customer_id}`);
    $("commerce-bot").replaceChildren();
    for (const row of instances.filter(row => !row.enabled && !bindings.some(b => b.instance_id === row.id))) option($("commerce-bot"), row.id, row.id);
    const prices = await bridge.apiGet("commerce-rates");
    rates = prices.rates; providers = prices.providers;
    const previousProvider = $("commerce-provider").value;
    $("commerce-provider").replaceChildren();
    for (const row of providers) option($("commerce-provider"), row.id, `${row.id} · ${row.model}`);
    if (providers.some(row => row.id === previousProvider)) $("commerce-provider").value = previousProvider;
    rateFields();
    await usage();
    $("commerce-status").textContent = rates.some(row => row.value.verified && providers.some(provider =>
      provider.id === row.provider_id && provider.model === row.model && provider.fingerprint === row.fingerprint))
      ? "已保存核验费率。既有周期不重新计价。" : "商业 AI 未启用：等待中转账单、计数与请求上限核验。";
  }
  function rateFields() {
    const provider = providers.find(row => row.id === $("commerce-provider").value);
    const rate = rates.find(row => row.provider_id === provider?.id && row.model === provider?.model);
    const matching = rate && rate.fingerprint === provider?.fingerprint;
    for (const [id, key] of [["commerce-input-price", "input_cny_per_million"], ["commerce-cache-price", "cached_input_cny_per_million"], ["commerce-output-price", "output_cny_per_million"]]) $(id).value = rate?.value[key] || "0";
    $("commerce-rate-evidence").value = matching ? rate.value.evidence_ref : "";
    for (const [id, key] of [["commerce-count-proof", "input_count_verified"], ["commerce-bound-proof", "output_bound_verified"], ["commerce-usage-proof", "usage_verified"], ["commerce-verified", "verified"]]) $(id).checked = matching && rate.value[key] === true;
    $("commerce-rate-confirm").checked = false;
  }
  $("commerce-create").onsubmit = event => {
    event.preventDefault(); action(async () => {
      if (!$("commerce-create-confirm").checked) throw new Error("explicit_confirmation_required");
      const result = await mutate("commerce-customers", {action: "create", version: 0, label: $("commerce-label").value});
      await refresh(); $("commerce-customer").value = result.customer_id;
      $("commerce-label").value = ""; $("commerce-create-confirm").checked = false; await usage();
    });
  };
  $("commerce-order").onsubmit = event => {
    event.preventDefault(); action(async () => {
      const row = customer(), operation = $("commerce-action").value;
      if (!row || !$("commerce-confirm").checked) throw new Error("explicit_confirmation_required");
      const payload = {customer_id: row.id, version: row.version, action: operation};
      let route = "commerce-customers";
      if (["activate", "renew"].includes(operation)) { route = "commerce-subscriptions"; payload.plan = $("commerce-plan").value; }
      if (operation === "topup") { route = "commerce-topups"; payload.action = "add"; payload.product = $("commerce-product").value; }
      if (["activate", "renew", "topup"].includes(operation)) {
        if (!$("commerce-paid").checked || !$("commerce-receipt").value.trim()) throw new Error("payment_confirmation_required");
        payload.paid_confirmed = true; payload.receipt_ref = $("commerce-receipt").value.trim();
      }
      if (operation === "bind") {
        const bot = instances.find(bot => bot.id === $("commerce-bot").value);
        if (!bot) throw new Error("instance_not_selected");
        payload.instance_id = bot.id; payload.session_ref = bot.session_ref;
      }
      await mutate(route, payload);
      $("commerce-confirm").checked = $("commerce-paid").checked = false;
      $("commerce-receipt").value = ""; await refresh();
    });
  };
  $("commerce-reconcile").onsubmit = event => {
    event.preventDefault(); action(async () => {
      const row = customer();
      if (!row || !$("commerce-reconcile-confirm").checked) throw new Error("explicit_confirmation_required");
      await mutate("commerce-reconcile", {customer_id: row.id, version: row.version,
        action: $("commerce-reconcile-action").value, call_id: $("commerce-call").value,
        actual_cny: $("commerce-actual").value, evidence_ref: $("commerce-evidence").value});
      $("commerce-reconcile-confirm").checked = false; await refresh();
    });
  };
  $("commerce-rate").onsubmit = event => {
    event.preventDefault(); action(async () => {
      const provider = providers.find(row => row.id === $("commerce-provider").value);
      if (!provider || !$("commerce-rate-confirm").checked) throw new Error("explicit_confirmation_required");
      const previous = rates.find(row => row.provider_id === provider.id && row.model === provider.model);
      await mutate("commerce-rates", {action: "save", version: previous?.version || 0,
        provider_id: provider.id, model: provider.model, rate: {
          input_cny_per_million: $("commerce-input-price").value,
          cached_input_cny_per_million: $("commerce-cache-price").value,
          output_cny_per_million: $("commerce-output-price").value,
          evidence_ref: $("commerce-rate-evidence").value,
          verified: $("commerce-verified").checked,
          input_count_verified: $("commerce-count-proof").checked,
          output_bound_verified: $("commerce-bound-proof").checked,
          usage_verified: $("commerce-usage-proof").checked,
        }});
      await refresh();
    });
  };
  $("commerce-action").onchange = fields;
  $("commerce-customer").onchange = () => { page = 1; action(usage); };
  $("commerce-provider").onchange = rateFields;
  $("commerce-refresh").onclick = () => action(refresh);
  $("commerce-previous").onclick = () => { if (page > 1) { page--; action(usage); } };
  $("commerce-next").onclick = () => { page++; action(usage); };
  fields();
  window.LajiaoCommerce = {
    setAccess(api, enabled, bots) { bridge = api; allowed = enabled; instances = bots; },
    refresh,
  };
})();
