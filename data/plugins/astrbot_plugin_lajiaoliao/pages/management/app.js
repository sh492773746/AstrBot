(() => {
  const $ = id => document.getElementById(id);
  const defaultAutomation = {
    enabled: false, keyword_enabled: true, flood_enabled: true, ai_enabled: false,
    ai_rules: "", ai_min_confidence: 0.95, flood_limit: 8, flood_window_seconds: 10,
    duplicate_limit: 5, duplicate_window_seconds: 30, violation_window_hours: 24,
    mute_minutes: [5, 15, 60], cooldown_seconds: 60,
    punishment_mode: "mute", flood_recall: false,
  };
  let bridge, instances = [], current, policy, groups = new Map(), page = 1, busy = false, dirty = false, discard;
  let groupState = null, pendingAction = null;
  const pendingActions = new Map();
  let businessState = null, rankingPage = 1, auditBefore = 0, businessDirty = false;
  let capabilityState = null;
  let aiState = null, aiDirty = false, aiPage = 1;
  let activeMain = "controls";
  let commerceOperator = false;
  let managersDirty = false, managerDraft = new Set(), users = [], userPage = 1, usersLoaded = false, usersMore = false;
  const errors = {
    session_access_denied: "没有此账号的访问权限。",
    session_binding_mismatch: "账号与机器人绑定不一致，请检查账号。",
    policy_changed_refresh_required: "权限已被其他页面修改，请刷新后重新设置。",
    policy_save_failed: "保存失败，原有权限未改变。",
    instance_changed_refresh_required: "机器人设置已更新，请刷新后重新设置。",
    message_policy_denied: "此群未获消息权限，请先保存群白名单。",
    instance_offline: "机器人未在线，请先在机器人设置中启用并连接。",
    nickname_changed_refresh_required: "群名片已被修改，请刷新群状态。",
    own_unrevoked_message_required: "只能撤回此机器人自己发送且尚未撤回的消息。",
    quote_not_found: "没有找到该消息，可能已超出最近 200 条范围。",
    action_receipt_not_found: "暂未查到回执。请核验群内实际结果，不要重复操作。",
    business_changed_refresh_required: "群业务设置已更新，请刷新后重新编辑。",
    lottery_settings_frozen: "本期抽奖已冻结参数，请结束活动后修改。",
    schedule_result_requires_review: "任务结果未知，请人工核验，未重复发送。",
    native_admin_required: "机器人不是此群的平台管理员，未执行操作。",
    native_action_not_authorized: "此群未授权该动作，请先保存逐群动作授权。",
    protected_member: "禁止处罚管理员或已绑定机器人。",
    protected_or_unverified_member: "目标不是可核实的普通成员，未执行。",
    announcement_changed_refresh_required: "群公告已变化，请刷新后重新确认。",
    explicit_kick_confirmation_required: "踢人需要单独确认。",
    group_manager_member_required: "只能授权此群已核实的成员，不能授权机器人账号。",
    invalid_group_managers: "管理员 UID 格式不正确、重复或超过 100 人。",
    automation_requires_review_and_mute: "自动禁言需要同时开启内容审核和此群成员禁言授权。",
    automation_requires_review_provider: "请先选择已配置的 AI 审核模型。",
    automation_requires_ai_rules: "请填写用于 AI 判断的群规。",
    invalid_automation_rules: "自动化规则格式不正确，请刷新后重新设置。",
    invalid_automation_number: "自动化数值超出允许范围。",
    invalid_automation_confidence: "AI 处罚置信度必须为 0.90 至 1.00。",
    invalid_automation_duration: "三个禁言时长须递增，且每项不超过 60 分钟。",
    automatic_operation_not_found: "没有找到此群的自动处罚记录。",
    manual_group_actions_removed: "手动群操作已移除，请在当前群自动化设置中配置规则。",
    native_capability_unverified: "该能力尚未通过指定成员实测，执行保持关闭。",
    quiet_boundaries_equal: "禁言与解禁时间不能相同。",
    invalid_schedule_clock: "请填写有效的北京时间。",
    invalid_card_length: "名片前后缀和生成长度超出平台限制。",
    invalid_card_text: "名片前后缀不能包含控制字符。",
    lottery_start_must_be_future: "抽奖开始时间必须晚于当前时间。",
    lottery_disabled_or_incomplete: "请先开启抽奖并填写奖品和领奖联系人。",
    lottery_notification_capacity: "逐人通知的参与上限需设置为 1～100 人。",
    lottery_notification_unverified: "群引用回复或 GIF 发送尚未通过实测，暂不能启用。",
    empty_reminder_text: "开启通知计划前请填写通知内容。",
    invalid_reminder_text: "通知内容无效或超过 2000 个字符。",
    maintenance_action_grant_required: "此自动化缺少对应的逐群动作授权。",
    invitation_tracking_required: "邀请奖励和人数门槛须先开启可靠邀请跟踪。",
    schedule_limit_reached: "任务队列已满，请先核验未完成和未知任务。",
    dashboard_schedule_grants_required: "计划须由所属 Dashboard 用户明确保存。",
    ai_changed_refresh_required: "AI 配置已被修改，请刷新后重新编辑。",
    notification_admin_required: "提醒接收人必须是对应范围内已授权的管理员。",
    invalid_ai_persona: "人格不存在，请刷新人格列表。",
    distinct_ai_personas_required: "聊天人格与群管人格必须分开。",
    commerce_binding_required: "此机器人尚未分配有效套餐，请在套餐运营中核验。",
    commerce_subscription_inactive: "套餐未生效、已到期或服务已暂停。",
    commerce_group_limit: "当前套餐最多管理 10 个有效群。",
    commerce_ai_paused: "AI 助手暂不可用，固定群管不受影响。",
  };
  const statesText = {
    pending: "待执行", running: "执行中", succeeded: "已完成", failed: "失败",
    unknown: "结果未知", paused: "已暂停", cancelled: "已取消", opening: "开启中",
    open: "报名中", drawing: "开奖中", publishing: "发布中", finished: "已结束",
    active: "运行中", deleted: "已删除", sent: "发送成功", rejected: "已拒绝",
    unsupported: "不支持",
    skipped: "已跳过", sending: "发送中",
  };
  function executionVerified(feature) {
    return Object.hasOwn(feature, "execution_verified")
      ? feature.execution_verified === true : feature.live_acceptance === true;
  }
  function changed() {
    dirty = true; $("dirty").textContent = "有未保存的更改"; $("save").disabled = busy;
    $("tab-controls").dataset.dirty = "true";
  }
  async function action(fn) {
    if (busy) return;
    busy = true; $("status").textContent = "";
    const disabled = new Map([...document.querySelectorAll("button, input, select, textarea")]
      .map(el => [el, el.disabled]));
    disabled.forEach((wasDisabled, el) => { el.disabled = true; });
    try { await fn(); } catch (error) {
      $("status").textContent = errors[error.message] || "操作未完成，请刷新状态核验。";
    } finally {
      busy = false;
      disabled.forEach((wasDisabled, el) => { el.disabled = wasDisabled; });
      if (current) {
        render();
        renderActionGroups();
        renderUsers();
      }
    }
  }
  function render() {
    if (!policy) return;
    $("save").disabled = busy || !dirty || managersDirty;
    $("private").disabled = busy || managersDirty;
    const id = $("action-group").value, toggle = $("current-group-enabled");
    toggle.setAttribute("aria-label", id ? `允许群 ${id}` : "允许当前群");
    toggle.checked = Boolean(id && policy.group_ids.includes(id));
    toggle.disabled = busy || managersDirty || !current || !id;
  }
  function select(id) {
    current = instances.find(item => item.id === id);
    groups = new Map(); page = 1; dirty = false; $("dirty").textContent = ""; $("more").hidden = true;
    $("tab-controls").removeAttribute("data-dirty"); $("workspace").hidden = !current && !commerceOperator;
    groupState = null; pendingAction = pendingActions.get(id) || null;
    $("action-group").replaceChildren(); $("own-messages").replaceChildren(); $("notifications").replaceChildren(); $("action-history").replaceChildren();
    $("group-state").textContent = ""; $("action-result").textContent = ""; $("query-action").hidden = true;
    $("group-role").textContent = "待核实";
    if (pendingAction) receipt({status: "unknown", operation_id: pendingAction.operation_id});
    clearBusiness();
    if (!current) {
      $("identity").textContent = "暂无已创建且绑定当前用户的机器人";
      if (commerceOperator) activateMainTab($("tab-commerce"));
      return;
    }
    policy = { group_mode: "allowlist", group_ids: [...(current.message_policy.group_ids || [])],
      private_enabled: current.message_policy.private_enabled === true };
    $("private").checked = policy.private_enabled;
    const states = { stopped: "未连接", online: "在线", connecting: "连接中", error: "连接异常", authorization_required: "需要重新授权", reauth_required: "需要重新登录", token_expired: "凭据续期中", kicked_offline: "已被其他设备下线", protocol_incompatible: "协议不兼容", subscription_inactive: "套餐未生效" };
    $("identity").textContent = `账号 ${current.account_id} · ${current.enabled ? "已启用" : "已停用"}`;
    $("connection-state").textContent = states[current.connection_state] || current.connection_state;
    $("connection-state").dataset.online = String(current.connection_state === "online");
    $("allowed-groups").textContent = `${current.message_policy.group_ids.length} 个群`;
    activateMainTab($("main-tabs").querySelector(`[data-view="${activeMain}"]`));
    render();
    renderActionGroups();
  }
  async function refresh() {
    const previous = current?.id, previousGroup = $("action-group").value;
    const data = await bridge.apiGet("management"); instances = data.instances || [];
    commerceOperator = data.commerce_operator === true;
    $("tab-commerce").hidden = !commerceOperator;
    window.LajiaoCommerce?.setAccess(bridge, commerceOperator, instances);
    if (!commerceOperator && activeMain === "commerce") activeMain = "controls";
    $("instance-count").textContent = `${instances.length} 个机器人`;
    $("instance").replaceChildren();
    for (const item of instances) {
      const option = document.createElement("option"); option.value = item.id; option.textContent = item.id;
      $("instance").append(option);
    }
    $("instance").value = instances.some(item => item.id === previous) ? previous : (instances[0]?.id || "");
    await loadInstance($("instance").value, $("instance").value === previous ? previousGroup : "");
  }
  async function loadInstance(id, selectedGroup = "") {
    select(id);
    if (!current) { await loadActiveView(); return; }
    await directory();
    if ([...$("action-group").options].some(item => item.value === selectedGroup)) {
      $("action-group").value = selectedGroup;
      renderActionGroups();
    }
    await loadActiveView();
  }
  async function directory(more = false) {
    if (!current) return;
    const next = more ? page : 1;
    const data = await bridge.apiPost("groups", { session_ref: current.session_ref, page: next });
    const rows = data.groups ?? data.groupInfos ?? data.groupList;
    if (!Array.isArray(rows)) throw new Error("invalid_directory_response");
    const normalized = rows.map(row => [String(row.groupID ?? row.id ?? ""), String(row.groupName ?? row.name ?? "未命名群")]);
    if (normalized.some(row => !row[0])) throw new Error("invalid_directory_response");
    if (!more) groups = new Map();
    normalized.forEach(([id, name]) => groups.set(id, name)); page = next + 1;
    const total = Number(data.total ?? data.totalCount);
    $("more").hidden = rows.length < 100 || (Number.isFinite(total) && total <= next * 100);
    render();
    renderActionGroups();
  }
  function renderActionGroups() {
    const previous = $("action-group").value;
    $("action-group").replaceChildren();
    const all = new Map(groups);
    for (const id of [...(current?.message_policy.group_ids || []), ...(policy?.group_ids || [])]) {
      if (!all.has(id)) all.set(id, "群名待加载");
    }
    for (const [id, name] of all) {
      const option = document.createElement("option");
      option.value = id; option.textContent = `${name} · ${id}${policy?.group_ids.includes(id) ? "" : " · 未开启"}`;
      $("action-group").append(option);
    }
    if (!$("action-group").options.length) {
      const empty = document.createElement("option");
      empty.value = ""; empty.textContent = "暂无群聊";
      $("action-group").append(empty);
    }
    if ([...$("action-group").options].some(item => item.value === previous)) $("action-group").value = previous;
    $("action-group").disabled = busy || !current || !all.size;
    $("groups").disabled = busy || !current;
    render();
  }
  function selectedGroupEnabled() {
    return Boolean(current && $("action-group").value &&
      current.message_policy.group_ids.includes($("action-group").value));
  }
  function unavailableGroupMessage() {
    return $("action-group").value ? "当前群未开启，请先保存群聊权限" : "暂无群聊";
  }
  function clearBusiness() {
    aiState = null; aiDirty = false; aiPage = 1; $("ai-form").hidden = true;
    $("ai-status").textContent = ""; $("tab-ai").removeAttribute("data-dirty");
    businessState = null; rankingPage = 1; auditBefore = 0; businessDirty = false;
    $("tab-business-settings").removeAttribute("data-dirty");
    $("business-form").hidden = true; $("business-status").textContent = "";
    managersDirty = false; managerDraft = new Set(); users = []; userPage = 1; usersLoaded = false; usersMore = false;
    $("user-search").value = ""; $("users-status").textContent = "";
    $("managers-dirty").textContent = "授权已同步"; $("tab-managers").removeAttribute("data-dirty");
    renderUsers();
    ["rankings-list", "activities-list", "reminders-list", "audit-list", "capabilities-list", "invitation-list"].forEach(id => $(id).replaceChildren());
    $("automation-status-summary").textContent = unavailableGroupMessage();
  }
  async function loadBusiness() {
    const group = $("action-group").value;
    if (!selectedGroupEnabled()) {
      $("business-status").textContent = unavailableGroupMessage(); return;
    }
    if (businessDirty || managersDirty) {
      $("business-status").textContent = "有未保存的设置或授权，请先保存再刷新";
      return;
    }
    capabilityState = await bridge.apiGet("capabilities");
    const data = await bridge.apiPost("business", {session_ref: current.session_ref, group_id: group,
      ranking_page: rankingPage, audit_before: auditBefore});
    businessState = {...data, instance_id: current.id};
    managerDraft = new Set(data.settings.manager_user_ids || []);
    const settings = data.settings, lottery = settings.lottery;
    $("group-ai-enabled").checked = settings.group_ai_enabled === true;
    $("commands-enabled").checked = settings.commands_enabled !== false;
    $("astrbot-ai-status").textContent = `AstrBot 配置文件：${data.astrbot_ai?.profile_name || "默认"} · ${
      data.astrbot_ai?.provider_enabled === false ? "AI 已关闭" : "AI 已启用"}`;
    $("invitations-enabled").checked = settings.invitations_enabled === true;
    $("invitation-rewards-enabled").checked = settings.invitation_rewards_enabled === true;
    $("lottery-plan-enabled").checked = settings.lottery_plan?.enabled === true;
    const start = settings.lottery_plan?.starts_at || 0;
    $("lottery-start").value = start ? new Date(start * 1000 + 8 * 3600000).toISOString().slice(0, 16) : "";
    $("lottery-min-invitations").value = lottery.min_invitations || 0;
    $("lottery-notify-results").checked = lottery.notify_results === true;
    const features = capabilityState?.features || [];
    $("lottery-notify-results").disabled = !features.some(
      item => item.name === "quote" && item.live_group_send
    ) || !features.some(
      item => item.name === "image" && item.live_group_send && item.formats_send?.includes("GIF")
    );
    $("reminder-plan-enabled").checked = settings.reminder_plan?.enabled === true;
    $("reminder-clock").value = settings.reminder_plan?.clock || "09:00";
    $("reminder-text").value = settings.reminder_plan?.text || "";
    const maintenance = settings.maintenance || {};
    for (const [id, field] of [["quiet-enabled", "quiet_enabled"], ["cards-enabled", "cards_enabled"], ["cleanup-enabled", "cleanup_enabled"], ["reply-recall-enabled", "reply_recall_enabled"]]) $(id).checked = maintenance[field] === true;
    $("quiet-start").value = maintenance.quiet_start || "23:00";
    $("quiet-end").value = maintenance.quiet_end || "07:00";
    $("card-characters").value = maintenance.name_characters || 2;
    $("card-prefix").value = maintenance.prefix || "";
    $("card-suffix").value = maintenance.suffix || "";
    const verified = new Set((capabilityState?.features || []).filter(executionVerified).map(item => item.name));
    $("image-capability").textContent = verified.has("image") ? "已实测" : "已实现，待实测";
    $("cards-enabled").disabled = !verified.has("rename_others");
    $("cleanup-enabled").disabled = !verified.has("cleanup");
    $("automation-flood-recall").disabled = !verified.has("revoke_others");
    $("automation-mode").options[1].disabled = !verified.has("revoke_others");
    $("maintenance-capability-status").textContent = verified.has("rename_others") && verified.has("revoke_others")
      ? "成员撤回与名片接口已验证；账号清理缺少权威状态接口，保持关闭。"
      : "尚未验证的原生动作保持关闭。";
    $("scheduled-list").replaceChildren();
    for (const job of data.scheduled || []) {
      const row = document.createElement("p");
      row.textContent = `${({quiet:"群作息",lottery:"抽奖",notification:"通知",reply_recall:"回复撤回"})[job.feature] || job.feature} · ${new Date(job.due * 1000).toLocaleString("zh-CN", {timeZone:"Asia/Shanghai"})} · ${statesText[job.state] || job.state}`;
      $("scheduled-list").append(row);
    }
    $("invitation-points").value = settings.invitation_points ?? 1;
    for (const field of ["rankings", "lottery", "reminders", "review"]) $(field + "-enabled").checked = settings[field + "_enabled"];
    for (const field of ["prize", "contact", "winners", "minutes", "capacity"]) $("lottery-" + field).value = lottery[field];
    $("review-provider").replaceChildren(new Option("不使用 AI 审核", ""));
    for (const model of data.review_models || []) $("review-provider").add(new Option(model.id, model.id));
    if (settings.review_provider_id && ![...$("review-provider").options].some(o => o.value === settings.review_provider_id)) {
      $("review-provider").add(new Option(`${settings.review_provider_id}（待核验）`, settings.review_provider_id));
    }
    $("review-provider").value = settings.review_provider_id;
    $("review-keywords").value = settings.keywords.join("\n");
    const automation = settings.automation ||= structuredClone(defaultAutomation);
    $("automation-enabled").checked = automation.enabled;
    $("automation-keyword").checked = automation.keyword_enabled;
    $("automation-flood").checked = automation.flood_enabled;
    $("automation-ai").checked = automation.ai_enabled;
    $("automation-mode").value = automation.punishment_mode || "mute";
    $("automation-flood-recall").checked = automation.flood_recall === true;
    $("automation-status-summary").textContent = automation.enabled
      ? `当前群自动递进禁言已开启 · ${automation.ai_enabled ? "AI 规则已启用" : "仅使用关键词/刷屏规则"}`
      : "当前群自动处罚已关闭；规则开关仅在保存并确认后生效。";
    $("ai-management-enabled").checked = settings.ai_management_enabled;
    $("automation-ai-rules").value = automation.ai_rules;
    for (const [id, field] of [
      ["confidence", "ai_min_confidence"], ["flood-limit", "flood_limit"],
      ["flood-window", "flood_window_seconds"], ["duplicate-limit", "duplicate_limit"],
      ["duplicate-window", "duplicate_window_seconds"], ["count-window", "violation_window_hours"],
      ["cooldown", "cooldown_seconds"],
    ]) $("automation-" + id).value = automation[field];
    automation.mute_minutes.forEach((minutes, index) => $("automation-mute-" + index).value = minutes);
    $("business-form").hidden = false;
    $("business-status").textContent = `业务版本 ${data.version}`;
    $("ranking-page").textContent = `第 ${rankingPage} 页`;
    $("rankings-list").replaceChildren();
    for (const [index, item] of (data.rankings || []).entries()) {
      const row = document.createElement("p"); row.textContent = `${(rankingPage - 1) * 20 + index + 1}. ${item.name}（${item.member_id}）· ${item.count} 条`;
      $("rankings-list").append(row);
    }
    if (!data.rankings?.length) $("rankings-list").textContent = "暂无记录";
    $("activities-list").replaceChildren();
    for (const item of data.activities || []) {
      const row = document.createElement("p");
      row.textContent = `${item.parameters.prize} · ${statesText[item.state] || item.state} · ${item.enrolled} 人报名 · ${item.id}`;
      if (Object.keys(item.notifications || {}).length) {
        row.textContent += " · 通知步骤：" + Object.entries(item.notifications)
          .map(([state, count]) => `${statesText[state] || state} ${count}`).join("，");
      }
      $("activities-list").append(row);
    }
    if (!data.activities?.length) $("activities-list").textContent = "暂无活动";
    $("reminders-list").replaceChildren();
    for (const item of data.reminders || []) {
      const row = document.createElement("div"); row.className = "message-row";
      const text = document.createElement("span");
      text.textContent = `${item.clock} · ${statesText[item.state] || item.state}\n${item.text}\n${item.id}`;
      const button = document.createElement("button"); button.textContent = "暂停";
      button.onclick = () => action(async () => {
        await bridge.apiPost("business", {session_ref: current.session_ref, group_id: group,
          action: "pause-reminder", task_id: item.id, confirm: true,
          version: businessState.version, instance_revision: businessState.instance_revision});
        await loadBusiness();
      });
      row.append(text, button); $("reminders-list").append(row);
    }
    if (!data.reminders?.length) $("reminders-list").textContent = "暂无任务";
    $("audit-list").replaceChildren();
    for (const item of data.audit || []) {
      const row = document.createElement("p");
      row.textContent = `${new Date(item.stamp * 1000).toLocaleString("zh-CN", {timeZone: "Asia/Shanghai"})} · ${item.kind} · ${item.status}\n${item.subject}${item.detail ? " · " + item.detail : ""}`;
      $("audit-list").append(row);
    }
    if (!data.audit?.length) $("audit-list").textContent = "暂无记录";
    $("automatic-list").replaceChildren();
    const outcomes = {succeeded: "成功", rejected: "拒绝", failed: "失败", unsupported: "不支持", unknown: "结果未知"};
    const reasons = {keyword: "关键词", flood: "刷屏", ai_violation: "AI 群规"};
    for (const item of data.automatic || []) {
      const row = document.createElement("div"); row.className = "message-row";
      const text = document.createElement("span");
      text.textContent = `${item.user_id} · ${reasons[item.reason] || item.reason} · ${outcomes[item.status] || item.status}\n${item.muted_seconds / 60} 分钟 · ${item.operation_id}`;
      row.append(text);
      if (item.status === "unknown") {
        const button = document.createElement("button"); button.className = "icon-button";
        button.title = "查询处罚回执"; button.setAttribute("aria-label", button.title);
        const icon = document.createElement("img"); icon.src = "./icons/refresh-cw.svg"; icon.className = "icon"; icon.alt = "";
        button.append(icon); button.onclick = () => action(async () => {
          await bridge.apiPost("business", {session_ref: current.session_ref, group_id: group, action: "refresh-automatic", operation_id: item.operation_id});
          await loadBusiness();
        }); row.append(button);
      }
      $("automatic-list").append(row);
    }
    if (!data.automatic?.length) $("automatic-list").textContent = "暂无自动处罚";
    $("invitation-list").replaceChildren();
    for (const item of data.invitations || []) {
      const row = document.createElement("p"); row.textContent = `${item.inviter_id} · ${item.count} 人 · ${item.points} 积分`;
      $("invitation-list").append(row);
    }
    if (!data.invitations?.length) $("invitation-list").textContent = "暂无已核实的新邀请";
    $("capabilities-list").replaceChildren();
    const capabilityRows = [...(capabilityState?.features || []), ...(capabilityState?.business || [])];
    for (const item of capabilityRows) {
      const executable = executionVerified(item);
      let status = item.live_acceptance ? "已实测" : "已实现 · 待实测";
      if (item.state?.startsWith("partial_live")) status = "部分实测";
      if (item.state?.startsWith("blocked")) status = "待平台核验 · 执行关闭";
      if (item.state === "unverified" || item.state === "implemented_live_gate_closed") status = "待验证 · 执行关闭";
      if (item.state === "excluded_by_scope") status = "本轮不接入";
      if (item.state === "assessment_only") status = "仅评估 · 未启用";
      if (item.execution_verified === true && item.live_acceptance !== true) status = "原生动作已验证";
      const row = document.createElement("div"); row.className = "capability-row";
      row.dataset.capability = item.name;
      const description = document.createElement("div"), title = document.createElement("strong");
      title.textContent = item.title;
      const detail = document.createElement("span"); detail.className = "muted";
      detail.textContent = item.ui_reason || (
        item.execution_verified === true && item.live_acceptance !== true
          ? "自动化可配置，默认关闭；真人完整闭环待验收。"
          : item.live_acceptance ? "已完成所列范围实测。" : status
      );
      const badge = document.createElement("span"); badge.className = "badge";
      if (!executable) badge.classList.add("muted");
      badge.textContent = status;
      description.append(title, detail);
      row.append(description, badge);
      $("capabilities-list").append(row);
    }
    if (!capabilityRows.length) {
      for (const name of Object.values(data.native_actions || {})) {
        const row = document.createElement("p"); row.textContent = `${name} · 状态待刷新`;
        $("capabilities-list").append(row);
      }
    }
  }
  function renderUsers() {
    $("users-list").replaceChildren();
    $("save-managers").disabled = busy || !managersDirty || !selectedGroupEnabled();
    $("users-prev").disabled = busy || userPage <= 1;
    $("users-next").disabled = busy || !usersMore;
    $("users-page").textContent = usersLoaded ? `第 ${userPage} 页` : "";
    $("business-form").querySelectorAll("input, select, textarea, button").forEach(el => {
      el.disabled = busy || managersDirty;
    });
    const verified = new Set((capabilityState?.features || []).filter(executionVerified).map(item => item.name));
    for (const [id, feature] of [["cards-enabled","rename_others"],["cleanup-enabled","cleanup"],["automation-flood-recall","revoke_others"]]) {
      $(id).disabled = busy || managersDirty || !verified.has(feature);
    }
    $("automation-mode").options[1].disabled = !verified.has("revoke_others");
    const rows = new Map(users.map(user => [user.user_id, user]));
    for (const uid of managerDraft) if (!rows.has(uid)) {
      rows.set(uid, {user_id: uid, nickname: uid, identity: "group_manager"});
    }
    const names = {bot: "机器人账号", astrbot_admin: "AstrBot 管理员", group_manager: "本群机器人管理员", member: "普通用户"};
    const roles = {20: "普通成员", 60: "平台群管理员", 100: "平台群主"};
    const search = $("user-search").value.trim().toLowerCase();
    for (const user of rows.values()) {
      if (!`${user.user_id} ${user.nickname}`.toLowerCase().includes(search)) continue;
      const fixed = ["bot", "astrbot_admin"].includes(user.identity);
      const label = document.createElement("label"); label.className = "switch user-row";
      const text = document.createElement("span"), title = document.createElement("span");
      title.textContent = user.nickname;
      const details = document.createElement("small");
      details.textContent = `UID ${user.user_id} · ${roles[user.native_role] || "未在本页成员目录"}`;
      const badge = document.createElement("span"); badge.className = "badge";
      const saved = (businessState?.settings.manager_user_ids || []).includes(user.user_id);
      const kind = fixed ? user.identity : (saved ? "group_manager" : "member");
      badge.textContent = !fixed && saved !== managerDraft.has(user.user_id)
        ? (managerDraft.has(user.user_id) ? "待授权" : "待撤销") : names[kind];
      if (["bot", "member"].includes(kind)) badge.classList.add("muted");
      text.append(title, details, badge);
      const toggle = document.createElement("input"); toggle.type = "checkbox";
      toggle.setAttribute("aria-label", `授权群管理员 ${user.user_id}`);
      toggle.checked = user.identity === "astrbot_admin" || managerDraft.has(user.user_id);
      toggle.disabled = busy || fixed || !selectedGroupEnabled();
      toggle.onchange = () => {
        if (businessDirty || dirty) {
          $("users-status").textContent = "请先保存业务设置及消息权限"; renderUsers(); return;
        }
        if (toggle.checked) managerDraft.add(user.user_id); else managerDraft.delete(user.user_id);
        managersDirty = true; $("tab-managers").dataset.dirty = "true";
        $("managers-dirty").textContent = "有未保存的授权变更"; renderUsers();
      };
      label.append(text, toggle); $("users-list").append(label);
    }
    if (usersLoaded && !$("users-list").childElementCount) $("users-list").textContent = "暂无匹配的成员";
  }
  async function loadUsers() {
    if (!selectedGroupEnabled()) { $("users-status").textContent = unavailableGroupMessage(); return; }
    const data = await bridge.apiPost("group-users", {
      session_ref: current.session_ref, group_id: $("action-group").value, page: userPage,
    });
    if (!Array.isArray(data.members) || data.group_id !== $("action-group").value || data.instance_id !== current.id) {
      throw new Error("invalid_directory_response");
    }
    users = data.members; usersMore = data.has_more === true; usersLoaded = true;
    $("users-status").textContent = `本页 ${users.length} 位成员 · 已授权 ${managerDraft.size} 位群管理员`;
    renderUsers();
  }
  function activateTab(container, panels, button, moveFocus = false) {
    if (!button) return;
    document.querySelectorAll(panels).forEach(panel => {
      panel.hidden = panel.id !== button.dataset.view;
    });
    $(container).querySelectorAll("[role='tab']").forEach(tab => {
      const selected = tab === button;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
    });
    if (moveFocus) button.focus();
  }
  function activateMainTab(button, moveFocus = false) {
    if (!button) return;
    activeMain = button.dataset.view;
    activateTab("main-tabs", "[data-main-panel]", button, moveFocus);
    const businessView = !["controls", "ai-settings", "commerce"].includes(activeMain);
    $("load-business").hidden = !businessView;
    $("business-status").hidden = !businessView;
  }
  async function loadActiveView() {
    if (activeMain === "commerce") { await window.LajiaoCommerce.refresh(); return; }
    if (!current || activeMain === "controls") return;
    if (activeMain === "ai-settings") { await loadAI(); return; }
    if (!selectedGroupEnabled()) {
      $("business-status").textContent = unavailableGroupMessage(); return;
    }
    if (["group-actions", "records"].includes(activeMain) && !groupState) await loadGroup();
    if (!businessState) await loadBusiness();
    if (activeMain === "managers" && !usersLoaded) await loadUsers();
  }
  function bindTabs(container, activate) {
    $(container).onclick = event => {
      const button = event.target.closest("[role='tab']"); if (!button || busy) return;
      activate(button);
    };
    $(container).onkeydown = event => {
    if (busy) return;
    const tabs = [...$(container).querySelectorAll("[role='tab']")].filter(tab => !tab.hidden);
    const currentIndex = tabs.indexOf(event.target.closest("[role='tab']"));
    if (currentIndex < 0) return;
    let nextIndex = currentIndex;
    if (event.key === "ArrowRight" || event.key === "ArrowDown") nextIndex = (currentIndex + 1) % tabs.length;
    else if (event.key === "ArrowLeft" || event.key === "ArrowUp") nextIndex = (currentIndex - 1 + tabs.length) % tabs.length;
    else if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = tabs.length - 1;
    else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault(); activate(tabs[currentIndex], true); return;
    } else return;
    event.preventDefault();
    activate(tabs[nextIndex], true);
    };
  }
  bindTabs("main-tabs", (button, focus) => {
    activateMainTab(button, focus);
    action(loadActiveView);
  });
  bindTabs("business-tabs", (button, focus) => activateTab("business-tabs", "[data-business-panel]", button, focus));
  bindTabs("record-tabs", (button, focus) => activateTab("record-tabs", "[data-record-panel]", button, focus));
  $("business-form").oninput = () => {
    businessDirty = true; $("tab-business-settings").dataset.dirty = "true";
    $("business-status").textContent = "有未保存的业务设置";
  };
  async function loadAI() {
    if (!current || aiDirty) {
      $("ai-status").textContent = aiDirty ? "请先保存当前 AI 设置" : "";
      return;
    }
    const group = selectedGroupEnabled() ? $("action-group").value : "";
    const data = await bridge.apiPost("ai-config", {session_ref: current.session_ref, group_id: group});
    aiState = data;
    $("ai-status").textContent = data.runner_supported === false
      ? "当前配置使用外部 Agent；人格分流与配额仅支持 AstrBot 本地 Agent。" : "";
    for (const [id, field] of [["ai-chat-persona", "chat_persona"], ["ai-management-persona", "management_persona"]]) {
      $(id).replaceChildren();
      for (const persona of data.personas) {
        const option = document.createElement("option");
        option.value = persona.id; option.textContent = persona.id; $(id).append(option);
      }
      $(id).value = data.settings[field];
    }
    for (const [id, candidates, selected] of [
      ["ai-private-recipient", data.private_admins, data.settings.private_notify_uid],
      ["ai-group-recipient", data.group_admins, data.group.settings.notify_uid],
    ]) {
      $(id).replaceChildren();
      const blank = document.createElement("option"); blank.value = ""; blank.textContent = "未指定"; $(id).append(blank);
      for (const uid of [...new Set([...(candidates || []), ...(selected ? [selected] : [])])]) {
        const option = document.createElement("option"); option.value = uid;
        option.textContent = `${uid}${(candidates || []).includes(uid) ? "" : " · 待核验"}`; $(id).append(option);
      }
      $(id).value = selected;
    }
    $("ai-user-limit").value = data.settings.user_daily_limit;
    $("ai-management-limit").value = data.settings.management_daily_limit;
    $("ai-group-limit").value = data.group.settings.daily_limit;
    $("ai-group-fields").hidden = !group; $("ai-form").hidden = false;
    $("ai-dirty").textContent = "配置已同步";
    await loadAIUsage();
  }
  async function loadAIUsage() {
    const data = await bridge.apiPost("ai-usage", {
      session_ref: current.session_ref, group_id: aiState?.group_id || "", page: aiPage,
    });
    $("ai-group-used").textContent = data.group_used;
    $("ai-group-remaining").textContent = Math.max(0, aiState.group.settings.daily_limit - data.group_used);
    $("ai-reset").textContent = new Date(data.reset * 1000).toLocaleTimeString("zh-CN", {timeZone: "Asia/Shanghai", hour: "2-digit", minute: "2-digit"});
    $("ai-metrics").textContent = `今日模型调用 ${data.metrics.model_calls} 次（审核 ${data.metrics.review_calls} 次） · 输入 ${data.metrics.input_tokens} / 输出 ${data.metrics.output_tokens} Token · ${data.metrics.unknown_usage} 次未返回用量`;
    $("ai-page").textContent = aiPage;
    $("ai-prev").disabled = aiPage <= 1;
    $("ai-next").disabled = data.usage.length < 50;
    $("ai-usage-list").replaceChildren();
    const labels = {user: "普通用户", management: "群管私聊", group: "单群"};
    for (const item of data.usage) {
      const row = document.createElement("p");
      row.textContent = `${labels[item.pool]} · ${item.subject} · 已用 ${item.used}/${item.limit} 次 · 剩余 ${item.remaining} 次`;
      $("ai-usage-list").append(row);
    }
    if (!data.usage.length) $("ai-usage-list").textContent = "今日暂无 AI 用量";
    $("ai-alert-list").replaceChildren();
    const states = {pending:"待通知", expired:"额度已重置，未补发", no_recipient:"无有效接收管理员", blocked:"权限或连接状态阻止", sending:"发送中", sent:"已通知", failed:"发送失败", rejected:"发送被拒绝", unsupported:"发送不支持", unknown:"待核验，未重复发送"};
    for (const item of data.notifications) {
      const row = document.createElement("p");
      row.textContent = `${labels[item.pool]} · ${item.used}/${item.quota_limit} · ${states[item.state] || item.state}`;
      $("ai-alert-list").append(row);
    }
    if (!data.notifications.length) $("ai-alert-list").textContent = "暂无额度告警";
  }
  $("ai-form").oninput = () => {
    aiDirty = true; $("ai-dirty").textContent = "有未保存的 AI 设置";
    $("tab-ai").dataset.dirty = "true";
  };
  $("ai-form").onsubmit = event => {
    event.preventDefault();
    action(async () => {
      await bridge.apiPost("ai-config", {
        session_ref: current.session_ref, group_id: aiState.group_id,
        action: "save", confirm: true, version: aiState.version,
        group_version: aiState.group.version, instance_revision: aiState.instance_revision,
        settings: {chat_persona: $("ai-chat-persona").value, management_persona: $("ai-management-persona").value,
          user_daily_limit: Number($("ai-user-limit").value), management_daily_limit: Number($("ai-management-limit").value),
          private_notify_uid: $("ai-private-recipient").value},
        group_settings: {daily_limit: Number($("ai-group-limit").value), notify_uid: $("ai-group-recipient").value},
      });
      aiDirty = false; $("tab-ai").removeAttribute("data-dirty");
      await loadAI(); $("ai-status").textContent = "AI 配置已保存";
    });
  };
  $("refresh-ai").onclick = () => action(loadAI);
  $("ai-prev").onclick = () => action(async () => { aiPage = Math.max(1, aiPage - 1); await loadAIUsage(); });
  $("ai-next").onclick = () => action(async () => { aiPage += 1; await loadAIUsage(); });
  $("business-form").onsubmit = event => {
    event.preventDefault(); if (!businessState || busy) return;
    if (managersDirty) { $("business-status").textContent = "请先保存管理员授权"; return; }
    action(async () => {
      const settings = structuredClone(businessState.settings);
      settings.group_ai_enabled = $("group-ai-enabled").checked;
      settings.commands_enabled = $("commands-enabled").checked;
      settings.invitations_enabled = $("invitations-enabled").checked;
      settings.invitation_rewards_enabled = $("invitation-rewards-enabled").checked;
      settings.lottery_plan = {enabled: $("lottery-plan-enabled").checked,
        starts_at: $("lottery-start").value ? Date.parse($("lottery-start").value + ":00+08:00") / 1000 : 0};
      settings.reminder_plan = {enabled: $("reminder-plan-enabled").checked, clock: $("reminder-clock").value, text: $("reminder-text").value.trim()};
      settings.maintenance = {
        quiet_enabled: $("quiet-enabled").checked, quiet_start: $("quiet-start").value, quiet_end: $("quiet-end").value,
        cards_enabled: $("cards-enabled").checked, name_characters: Number($("card-characters").value),
        prefix: $("card-prefix").value, suffix: $("card-suffix").value,
        cleanup_enabled: $("cleanup-enabled").checked, reply_recall_enabled: $("reply-recall-enabled").checked,
      };
      settings.lottery.min_invitations = Number($("lottery-min-invitations").value);
      settings.lottery.notify_results = $("lottery-notify-results").checked;
      settings.invitation_points = Number($("invitation-points").value);
      for (const field of ["rankings", "lottery", "reminders", "review"]) settings[field + "_enabled"] = $(field + "-enabled").checked;
      for (const field of ["prize", "contact"]) settings.lottery[field] = $("lottery-" + field).value.trim();
      for (const field of ["winners", "minutes", "capacity"]) settings.lottery[field] = Number($("lottery-" + field).value);
      settings.review_provider_id = $("review-provider").value.trim();
      settings.keywords = $("review-keywords").value.split("\n").map(word => word.trim()).filter(Boolean);
      settings.ai_management_enabled = $("ai-management-enabled").checked;
      const rules = settings.automation;
      rules.enabled = $("automation-enabled").checked;
      rules.keyword_enabled = $("automation-keyword").checked;
      rules.flood_enabled = $("automation-flood").checked;
      rules.ai_enabled = $("automation-ai").checked;
      rules.punishment_mode = $("automation-mode").value;
      rules.flood_recall = $("automation-flood-recall").checked;
      rules.ai_rules = $("automation-ai-rules").value.trim();
      for (const [id, field] of [
        ["confidence", "ai_min_confidence"], ["flood-limit", "flood_limit"],
        ["flood-window", "flood_window_seconds"], ["duplicate-limit", "duplicate_limit"],
        ["duplicate-window", "duplicate_window_seconds"], ["count-window", "violation_window_hours"],
        ["cooldown", "cooldown_seconds"],
      ]) rules[field] = Number($("automation-" + id).value);
      rules.mute_minutes = [0, 1, 2].map(index => Number($("automation-mute-" + index).value));
      if (rules.enabled || settings.maintenance.quiet_enabled || settings.maintenance.cards_enabled || settings.maintenance.cleanup_enabled || settings.lottery_plan.enabled || settings.reminder_plan.enabled || settings.maintenance.reply_recall_enabled) {
        const grants = [];
        if (rules.enabled) { settings.review_enabled = true; grants.push("mute"); }
        if (rules.enabled && (rules.punishment_mode === "recall_progressive" || rules.flood_recall)) grants.push("revoke_others");
        if (settings.maintenance.reply_recall_enabled) grants.push("reply_recall");
        if (settings.maintenance.quiet_enabled) grants.push("mute_all", "unmute_all");
        if (settings.maintenance.cards_enabled) grants.push("rename_others");
        if (settings.maintenance.cleanup_enabled) grants.push("cleanup");
        settings.native_actions = [...new Set([...settings.native_actions, ...grants])];
        $("automation-preview").textContent = `${current.id} · 群 ${businessState.group_id}\n自动禁言：${rules.enabled ? rules.mute_minutes.join(" / ") + " 分钟" : "关闭"}\n群作息：${settings.maintenance.quiet_enabled ? settings.maintenance.quiet_start + " 至 " + settings.maintenance.quiet_end : "关闭"}\n名片同步：${settings.maintenance.cards_enabled ? "开启" : "关闭"}\n不可用账号清理：${settings.maintenance.cleanup_enabled ? "开启（仅服务端确认状态）" : "关闭"}\n保存后仅安排未来任务。\n管理员和机器人受保护，不自动踢人。`;
        const dialog = $("automation-dialog");
        $("cancel-automation").disabled = false; $("confirm-automation").disabled = false;
        const accepted = await new Promise(resolve => {
          dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once: true});
          $("cancel-automation").onclick = () => dialog.close("cancel");
          $("confirm-automation").onclick = () => dialog.close("confirm");
          dialog.returnValue = ""; dialog.showModal();
        });
        if (!accepted) return;
      }
      await bridge.apiPost("business", {session_ref: current.session_ref, group_id: businessState.group_id,
        action: "save", confirm: true, settings, version: businessState.version, instance_revision: businessState.instance_revision});
      businessDirty = false; $("tab-business-settings").removeAttribute("data-dirty");
      await loadBusiness(); $("business-status").textContent = "业务设置已保存；已启用的计划将在预设时间自动运行";
    });
  };
  $("load-business").onclick = () => action(activeMain === "ai-settings" ? loadAI : loadBusiness);
  $("ranking-prev").onclick = () => action(async () => { rankingPage = Math.max(1, rankingPage - 1); await loadBusiness(); });
  $("ranking-next").onclick = () => action(async () => { rankingPage += 1; await loadBusiness(); });
  $("audit-more").onclick = () => action(async () => {
    const rows = businessState?.audit || []; if (!rows.length) return;
    auditBefore = rows[rows.length - 1].id; await loadBusiness();
  });
  async function loadGroup() {
    groupState = null; $("own-messages").replaceChildren(); $("notifications").replaceChildren(); $("action-history").replaceChildren();
    const group = $("action-group").value;
    if (!selectedGroupEnabled()) { $("group-state").textContent = unavailableGroupMessage(); return; }
    const data = await bridge.apiPost("group-state", {session_ref: current.session_ref, group_id: group});
    groupState = { ...data, instance_id: current.id };
    const roles = {20: "普通成员", 60: "群管理员", 100: "群主"};
    $("group-role").textContent = roles[data.role_level] || "待核实";
    $("group-state").textContent = `群 ${data.group_id} · ${roles[data.role_level] || "角色待核实"}`;
    for (const message of [...(data.messages || [])].reverse()) {
      const row = document.createElement("div"); row.className = "message-row";
      const text = document.createElement("span"); text.textContent = message.text;
      row.append(text); $("own-messages").append(row);
    }
    if (!data.messages?.length) $("own-messages").textContent = "暂无最近文字消息";
    for (const item of [...(data.notifications || [])].reverse()) {
      const row = document.createElement("p");
      const notices = {
        group_member_updated: `成员 ${item.user_id} 的群名片改为「${item.nickname}」`,
        message_revoked: `消息已撤回 · ${item.revoked_message_id}`,
        unsupported_media: `未接入或无效的媒体消息 · 类型 ${item.content_type}`,
        group_info_updated: `群资料更新 · ${item.announcement || ""}`,
        group_announcement_updated: `群公告更新 · ${item.announcement || ""}`,
        members_kicked: `成员被移出 · ${(item.member_ids || []).join("、")}`,
        members_invited: `成员被邀请 · ${(item.member_ids || []).join("、")}`,
        member_entered: `新成员入群 · ${(item.member_ids || []).join("、")}`,
        member_muted: `成员禁言 · ${item.user_id} · ${item.muted_seconds} 秒`,
        member_unmuted: `成员解禁 · ${item.user_id}`,
        group_muted: "全员禁言", group_unmuted: "解除全员禁言",
      };
      row.textContent = notices[item.action] || "群通知更新";
      $("notifications").append(row);
    }
    if (!data.notifications?.length) $("notifications").textContent = "暂无新通知";
    for (const item of data.receipts || []) {
      const row = document.createElement("p");
      const titles = {succeeded: "成功", rejected: "被拒绝", unknown: "结果未知"};
      const names = {revoke_self_message:"撤回自己的消息",set_self_group_nickname:"修改自己的群名片",mute:"成员禁言",unmute:"成员解禁",kick:"人工踢人",announce:"群公告",mute_all:"全员禁言",unmute_all:"解除全员禁言"};
      row.textContent = `${names[item.action] || item.action} · ${titles[item.status] || "结果未知"} · ${item.operation_id}`;
      $("action-history").append(row);
      if (item.status === "unknown" && !pendingAction) {
        pendingAction = {operation_id: item.operation_id, session_ref: current.session_ref, instance_id: current.id};
        receipt({status: "unknown", operation_id: item.operation_id});
      }
    }
    if (!data.receipts?.length) $("action-history").textContent = "暂无操作";
  }
  function receipt(data) {
    const titles = {succeeded: "操作成功", rejected: "操作被拒绝", unsupported: "不支持此操作",
      failed: "操作失败", unknown: "操作结果未知，请核验群内状态，不要重复执行"};
    $("action-result").textContent = `${titles[data.status] || titles.unknown} · ${data.operation_id || pendingAction?.operation_id || ""}${data.error ? " · " + (errors[data.error] || data.error) : ""}`;
    if (data.status === "unknown") {
      if (pendingAction) pendingActions.set(pendingAction.instance_id, pendingAction);
      $("query-action").hidden = false;
    } else {
      if (pendingAction) pendingActions.delete(pendingAction.instance_id);
      pendingAction = null; $("query-action").hidden = true;
    }
  }
  $("load-group").onclick = () => action(loadGroup);
  $("open-automation-settings").onclick = () => {
    $("automation-rules").open = true;
    const tab = $("tab-business-settings");
    activateMainTab(tab, true);
    action(loadActiveView);
  };
  $("action-group").onchange = () => {
    if (businessDirty || managersDirty || aiDirty) { $("action-group").value = aiState?.group_id || businessState?.group_id || ""; $("business-status").textContent = "请先保存当前群设置或管理员授权"; return; }
    groupState = null;
    clearBusiness();
    $("own-messages").replaceChildren(); $("notifications").replaceChildren(); $("action-history").replaceChildren();
    $("group-state").textContent = "";
    $("group-role").textContent = "待核实";
    action(loadActiveView);
  };
  $("query-action").onclick = () => action(async () => {
    if (!pendingAction) return;
    receipt(await bridge.apiPost("platform-action", {
      session_ref: pendingAction.session_ref, instance_id: pendingAction.instance_id,
      operation_id: pendingAction.operation_id, query: true,
    }));
  });
  $("instance").onchange = () => {
    if (dirty || businessDirty || managersDirty || aiDirty) {
      const next = $("instance").value;
      $("instance").value = current.id;
      discard = () => action(async () => { $("instance").value = next; await loadInstance(next); });
      $("discard-dialog").showModal(); return;
    }
    action(() => loadInstance($("instance").value));
  };
  $("refresh").onclick = () => {
    if (!dirty && !businessDirty && !managersDirty && !aiDirty) action(refresh);
    else { discard = () => action(refresh); $("discard-dialog").showModal(); }
  };
  $("cancel-discard").onclick = () => { discard = null; $("discard-dialog").close(); };
  $("confirm-discard").onclick = () => { $("discard-dialog").close(); discard?.(); discard = null; };
  $("private").onchange = () => { policy.private_enabled = $("private").checked; changed(); };
  $("current-group-enabled").onchange = () => {
    const id = $("action-group").value;
    if (!current || !id || busy) return;
    policy.group_ids = $("current-group-enabled").checked ? [...new Set([...policy.group_ids, id])]
      : policy.group_ids.filter(item => item !== id);
    changed(); renderActionGroups();
  };
  $("groups").onclick = () => action(() => directory(false));
  $("more").onclick = () => action(() => directory(true));
  $("add-form").onsubmit = event => {
    event.preventDefault(); if (!current || busy) return;
    if (businessDirty || managersDirty) { $("status").textContent = "请先保存当前群业务设置或管理员授权"; return; }
    const id = $("group-id").value.trim(); if (!id) return;
    groups.set(id, groups.get(id) || "手动添加"); policy.group_ids = [...new Set([...policy.group_ids, id])];
    $("group-id").value = ""; changed(); renderActionGroups();
    $("action-group").value = id;
    $("action-group").dispatchEvent(new Event("change"));
  };
  $("save").onclick = () => action(async () => {
    if (!current || !dirty) return;
    if (businessDirty || managersDirty) { $("status").textContent = "请先保存当前群业务设置或管理员授权"; return; }
    await bridge.apiPost("policy", { session_ref: current.session_ref, instance_id: current.id,
      revision: current.revision, message_policy: policy });
    await refresh(); $("dirty").textContent = "权限已保存";
  });
  $("load-users").onclick = () => action(loadUsers);
  $("user-search").oninput = renderUsers;
  $("users-prev").onclick = () => action(async () => { userPage = Math.max(1, userPage - 1); await loadUsers(); });
  $("users-next").onclick = () => action(async () => { userPage += 1; await loadUsers(); });
  $("save-managers").onclick = () => {
    if (!managersDirty || !businessState || busy) return;
    if (businessDirty || dirty) { $("status").textContent = "请先保存业务设置及消息权限"; return; }
    const previous = businessState.settings.manager_user_ids || [];
    const added = [...managerDraft].filter(uid => !previous.includes(uid));
    const removed = previous.filter(uid => !managerDraft.has(uid));
    $("manager-preview").textContent = `${current.id} · 群 ${businessState.group_id}\n新增授权：${added.join("、") || "无"}\n撤销授权：${removed.join("、") || "无"}`;
    $("manager-dialog").showModal(); $("cancel-managers").focus();
  };
  $("cancel-managers").onclick = () => { $("manager-dialog").close(); $("save-managers").focus(); };
  $("confirm-managers").onclick = () => {
    $("manager-dialog").close();
    action(async () => {
      await bridge.apiPost("business", {
        session_ref: current.session_ref, group_id: businessState.group_id,
        action: "save-managers", confirm: true, manager_user_ids: [...managerDraft],
        version: businessState.version, instance_revision: businessState.instance_revision,
      });
      managersDirty = false; $("tab-managers").removeAttribute("data-dirty");
      await loadBusiness(); await loadUsers();
      $("managers-dirty").textContent = "授权已保存";
    });
  };
  action(async () => { bridge = window.AstrBotPluginPage; await bridge.ready(); await refresh(); });
})();
