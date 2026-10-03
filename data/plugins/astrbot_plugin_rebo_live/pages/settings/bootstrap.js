// Keep recovery visible even when the module or bridge cannot initialize.
const pageState = document.getElementById('page-state');
const recovery = document.getElementById('reload-page');
const controls = document.getElementById('controls');
window.reboPageReady = false;
window.reboPageFailure = message => {
  window.reboPageReady = false;
  controls.disabled = true;
  pageState.textContent = message;
  recovery.hidden = false;
};
recovery.onclick = () => location.reload();
window.reboInitTimer = setTimeout(() => {
  if (!window.reboPageReady) window.reboPageFailure(
    '⚠️ 页面初始化超时：脚本或 WebUI 授权连接未就绪。请重新加载；仍失败请刷新外层 WebUI 并重新登录 WebUI，不必重新登录热播账号。'
  );
}, 15000);
window.addEventListener('error', () => window.reboPageFailure(
  '⚠️ 页面脚本加载或运行失败，操作已禁用。请重新加载页面；未自动重新登录或重试操作。'
));
window.addEventListener('unhandledrejection', () => window.reboPageFailure(
  '⚠️ 页面请求异常，操作已禁用。请重新加载并检查状态，不要重复提交。'
));
