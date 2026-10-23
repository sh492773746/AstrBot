// Run with playwright-cli run-code "$(cat dashboard/tests/wangshangliao.browser.js)".
// Start Vite on 127.0.0.1:3017 first. All API and captcha traffic is synthetic.
async (page) => {
  await page.setViewportSize({ width: 1280, height: 1000 });
  let saves = 0;
  const requests = [];
  await page.unroute('**/api/**');
  await page.unroute('http://127.0.0.1:3017/api/**');
  await page.route('http://127.0.0.1:3017/api/**', async route => {
    const path = route.request().url();
    const method = route.request().method();
    const input = method === 'POST' ? route.request().postDataJSON() : {};
    requests.push({ path, input });
    let data = {};
    if (path.includes('/registration')) {
      data = { instance_id: input.instance_id, registration_code: 'fixture-registration', captcha_id: 'fixture', expires_in: 600, status: 'pending' };
      if (input.action === 'login') Object.assign(data, { status: 'sms_required', resend_after: 60 });
      if (input.action === 'verify_sms') Object.assign(data, input.verification_code === '123456'
        ? { status: 'authenticated', session_ref: 'fixture-session-ref', account_id: '12', nickname: 'Fixture Bot' }
        : { status: 'sms_required', error: 'invalid_sms', resend_after: 45 });
      if (input.action === 'cancel') data.status = 'cancelled';
    } else if (path.includes('/config-profiles')) data = { profiles: [{ id: 'default', name: 'Default' }], config: {}, metadata: {} };
    else if (path.includes('/config-routes')) data = { routing: {} };
    else if (path.endsWith('/bots') && method === 'POST') {
      saves++;
      const config = input.config || input;
      if (config.password || config.validate_str || config.verification_code) throw new Error('Credential leaked into saved configuration');
      if (saves === 1) { await route.fulfill({ status: 500, json: { status: 'error', message: 'Fixture save failed' } }); return; }
    }
    await route.fulfill({ json: { status: 'ok', data, message: 'Fixture success' } });
  });
  await page.addInitScript(() => {
    window.initNECaptcha = (options, ready) => ready({ verify: () => { options.onVerify(new Error('fixture-retry')); options.onVerify(null, { validate: 'fixture-human' }); }, refresh() {}, destroy() {} });
  });
  await page.goto('http://127.0.0.1:3017/tests/fixtures/wangshangliao.html');
  await page.evaluate(() => localStorage.setItem('astrbot-locale', 'zh-CN'));
  await page.reload();
  await page.getByRole('combobox').click();
  await page.getByRole('listbox').getByText('旺商聊', { exact: true }).click();
  if (await page.getByRole('button', { name: '保存', exact: true }).isEnabled()) throw new Error('Save enabled before login');
  await page.getByRole('button', { name: '开始登录' }).click();
  await page.getByRole('textbox', { name: '账号 账号', exact: true }).fill('fixture-account');
  await page.getByRole('textbox', { name: '密码 密码', exact: true }).fill('fixture-password');
  await page.getByRole('textbox', { name: /启用群 ID/ }).fill('34');
  await page.screenshot({ path: 'docs/public/images/wangshangliao/login-zh.png', animations: 'disabled' });
  await page.getByRole('button', { name: '人工验证并登录' }).click();
  await page.getByRole('textbox', { name: /短信验证码/ }).fill('000000');
  if (await page.getByRole('button', { name: /重发短信/ }).isEnabled()) throw new Error('SMS cooldown missing');
  await page.getByRole('button', { name: '人工验证并登录' }).click();
  await page.getByText(/invalid_sms|短信验证码错误/).waitFor();
  await page.getByRole('textbox', { name: /短信验证码/ }).fill('123456');
  await page.getByRole('button', { name: '人工验证并登录' }).click();
  await page.getByText(/账号认证成功/).waitFor();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('button', { name: '保存', exact: true }).waitFor();
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('Fixture saved').waitFor();
  if (saves !== 2) throw new Error('Save retry did not reuse the registration');
  await page.evaluate(() => localStorage.setItem('astrbot-locale', 'en-US'));
  await page.reload();
  await page.getByRole('combobox').click();
  await page.getByRole('listbox').getByText('旺商聊', { exact: true }).click();
  await page.getByRole('button', { name: 'Start login' }).click();
  await page.getByRole('textbox', { name: 'Account Account', exact: true }).waitFor();
  await page.screenshot({ path: 'docs/public/images/wangshangliao/login-en.png', animations: 'disabled' });
  await page.getByRole('button', { name: 'Cancel', exact: true }).first().click();
  await page.getByRole('button', { name: 'Start login' }).waitFor();
  if (!requests.some(entry => entry.input?.action === 'cancel')) throw new Error('Cancellation was not sent');
  console.log('PASS: native creation, SMS rejection/cooldown, credential filtering, save retry, cancellation, English rendering');
}
