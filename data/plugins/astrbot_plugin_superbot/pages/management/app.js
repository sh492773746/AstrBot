(() => {
  const status = document.getElementById("status");
  const bridge = window.AstrBotPluginPage;
  if (bridge) {
    bridge.ready().then(context => {
      const operator = context?.username ? `当前登录：${context.username}` : "已通过 AstrBot 页面登录";
      status.textContent = `${operator} · 本页仅展示 Superbot 的群级管理边界`;
    }).catch(() => {
      status.textContent = "页面环境未就绪；说明内容仍可查看，修改配置请从群管理入口操作。";
    });
  } else {
    status.textContent = "未检测到 AstrBot 页面桥接；说明内容仍可查看。";
  }

  const tabs = [...document.querySelectorAll('[role="tab"]')];
  tabs.forEach(tab => tab.addEventListener("click", () => {
    tabs.forEach(item => {
      const active = item === tab;
      item.setAttribute("aria-selected", String(active));
      document.getElementById(`panel-${item.dataset.panel}`).hidden = !active;
    });
  }));
  document.getElementById("refresh").addEventListener("click", () => window.location.reload());
})();
