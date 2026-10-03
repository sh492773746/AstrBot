const el = id => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
try {
  if (!bridge || typeof bridge.ready !== 'function') throw new Error('bridge_missing');
  await Promise.race([bridge.ready(), new Promise((_,reject)=>setTimeout(()=>reject(new Error('bridge_timeout')),12000))]);
} catch (_) {
  window.reboPageFailure('⚠️ WebUI 授权连接未就绪或已超时。请重新加载；仍失败请重新登录外层 WebUI。不会自动重新登录热播账号。');
  throw new Error('Plugin bridge initialization failed');
}
async function request(endpoint, data) {
  let timer;
  try {
    return await Promise.race([
      data === undefined ? bridge.apiGet(endpoint) : bridge.apiPost(endpoint, data),
      new Promise((_,reject)=>{timer=setTimeout(()=>reject(new Error('request_timeout')),35000);}),
    ]);
  } finally { clearTimeout(timer); }
}
function fail(error, mutation=false) {
  const text = String(error?.message || '');
  const unauthorized = /401|403|unauthor|forbidden|token|未授权|登录过期|认证|授权/i.test(text);
  const message = unauthorized
    ? '⚠️ WebUI 授权失效或权限不足，请刷新外层 WebUI 并重新登录 WebUI。热播账号不会自动重新登录。'
    : text === 'request_timeout'
      ? '⚠️ 请求超时。' + (mutation ? '操作可能已经执行，请重新加载核对状态，不要重复提交。' : '暂时无法读取状态，请重新加载。')
      : '⚠️ 请求失败，无法确认操作或账号状态。请重新加载核对，未自动重试。';
  el('notice').textContent = message;
  window.reboPageFailure(message);
}
const cards = new Map();
let snapshot = null, busy = false;
const labels = {confirmed:'聊天室已回执（未核验公屏）',uncertain:'结果待确认',rejected:'被拒绝',sending:'发送中',reviewed:'已核查'};
const stamp = value => value ? new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}) : '暂无';
async function action(data) {
  if (busy || !window.reboPageReady) return false;
  busy = true;
  el('notice').textContent = '⏳ 正在处理…';
  try {
    const result = await request('action', data);
    if (result.status === 'error') throw new Error(result.message || '操作失败');
    el('notice').textContent = result.token ? '一次性授权链接（24小时有效）：https://t.me/dhcm1bot?start=' + result.token : '✅ 已完成';
    if (data.action === 'room') {
      const card = cards.get(data.id);
      if (card) card.dirty = false;
    }
    await status();
    return true;
  } catch(e) { fail(e,true); return false; }
  finally { busy = false; }
}
function button(label, fn) {
  const b = document.createElement('button'); b.textContent = label; b.onclick = fn; return b;
}
async function status() {
  try {
    const response = await request('status');
    if (response.status === 'error') throw new Error(response.message || 'status_error');
    snapshot = response.data ?? response;
    if (!snapshot || typeof snapshot.account !== 'string' || !snapshot.rooms || !snapshot.visible) throw new Error('invalid_status');
    el('account').textContent = '账号：' + snapshot.account + ' · ' + (snapshot.remember ? '已记住密码' : '未保存密码');
    const combined = {...snapshot.visible, ...snapshot.rooms};
    for (const [id, card] of cards) if (!(id in combined)) {card.node.remove();cards.delete(id);}
    for (const [id, visible] of Object.entries(combined)) {
      const room = snapshot.rooms[id] || {};
      let card = cards.get(id);
      if (!card) {
        const node = document.createElement('details'); node.style.padding = '16px 0'; node.style.borderBottom = '1px solid #d8dfea';
        const title = document.createElement('summary');
        const detail = document.createElement('p');
        const dirtyLabel = document.createElement('p');
        const text = document.createElement('textarea'); text.maxLength=300; text.placeholder = '输入一条新文案（1—300字）';
        const items = document.createElement('div');
        const text2 = document.createElement('textarea'); text2.hidden=true;
        const interval = document.createElement('input'); interval.type='number';interval.min=30;interval.max=86400;interval.setAttribute('aria-label','每轮间隔（秒）');
        const itemInterval = document.createElement('input'); itemInterval.type='number';itemInterval.min=1;itemInterval.max=3600;itemInterval.setAttribute('aria-label','每条间隔（秒）');
        const roundLabel=document.createElement('label');roundLabel.append('每轮间隔（30—86400秒）',interval);
        const itemLabel=document.createElement('label');itemLabel.append('每条间隔（1—3600秒）',itemInterval);
        const history = document.createElement('pre'); history.style.whiteSpace='pre-wrap';
        card = {node,title,detail,dirtyLabel,text,text2,items,interval,itemInterval,history,dirty:false}; cards.set(id,card);
        const change = () => {card.dirty=true;dirtyLabel.textContent='尚未保存';card.toggle.disabled=true;};
        text.oninput=change;text2.oninput=change;interval.oninput=change;itemInterval.oninput=change;
        const save=button('保存间隔设置',()=>action({action:'room',id,interval:Number(interval.value),item_interval:Number(itemInterval.value)}));
        const add=button('➕ 新增这一条',()=>action({action:'room',id,text_op:'add',value:text.value,revision:card.revision}));
        const discard=button('放弃修改',()=>{card.dirty=false;status();});
        card.toggle=button('启用',()=>action({action:'room',id,enabled:!snapshot.rooms[id]?.enabled}));
        card.remove=button('删除配置',()=>action({action:'delete',id}));
        card.ack=button('已核查，恢复发送',()=>action({action:'acknowledge',id}));
        card.invite=button('生成房间授权链接',()=>action({action:'invite',rooms:[id]}));
        node.append(title,detail,items,text,add,itemLabel,roundLabel,dirtyLabel,save,discard,card.toggle,card.remove,card.ack,card.invite,history);
        el('rooms').append(node);
      }
      card.title.textContent=(room.enabled?'🟢 ':'🔴 ')+(visible.title||id)+' · 主播：'+(snapshot.visible[id]?.anchor_name||room.anchor_name||'暂未获取');
      card.detail.textContent=(room.status||'尚未配置')+' · 最近发送：'+stamp(room.last_send)+' · 下次：'+stamp(snapshot.next_send[id]);
      if (!card.dirty) {
        card.revision=room.revision||0;card.text.value='';card.interval.value=room.interval||300;card.itemInterval.value=room.item_interval??2;card.dirtyLabel.textContent='';
        card.items.replaceChildren();
        (room.texts||[room.text,room.text2].filter(Boolean)).forEach((value,index)=>{
          const entry=document.createElement('div');
          const label=document.createElement('strong');label.textContent=`第${index+1}条`;
          const input=document.createElement('textarea');input.maxLength=300;input.value=value;
          input.oninput=()=>{card.dirty=true;card.dirtyLabel.textContent='尚未保存';};
          const revision=card.revision;
          entry.append(label,input,
            button('保存此条',()=>action({action:'room',id,text_op:'edit',index,value:input.value,revision})),
            button('删除此条',()=>{if(confirm(`删除第${index+1}条文案？`))action({action:'room',id,text_op:'delete',index,revision});}));
          card.items.append(entry);
        });
      }
      card.toggle.textContent=room.enabled?'🔴 暂停':'🟢 启用';
      card.toggle.disabled=card.dirty;
      card.remove.hidden=!(id in snapshot.rooms)||room.enabled||room.result==='sending';
      card.ack.hidden=true;
      card.invite.hidden=!(id in snapshot.rooms);
      card.history.textContent=(room.history||[]).slice().reverse().map(x=>stamp(x.time)+' · '+(labels[x.result]||x.result)+(x.reason?' · '+x.reason:'')).join('\n');
    }
    let empty=el('empty');
    if (!empty) {empty=document.createElement('p');empty.id='empty';el('rooms').append(empty);}
    empty.textContent=cards.size?'':snapshot.account==='已登录'?'暂无可见直播间':'请先登录账号';
    let grants=el('grants');
    if (!grants) {grants=document.createElement('section');grants.id='grants';el('controls').append(grants);}
    grants.replaceChildren();
    const heading=document.createElement('h2');heading.textContent='授权使用者';grants.append(heading);
    for (const [user, rooms] of Object.entries(snapshot.grants||{})) {
      const row=document.createElement('div');const name=document.createElement('p');name.textContent='用户 '+user;row.append(name);
      const selected=new Set(rooms);
      for (const [id,room] of Object.entries(snapshot.rooms)) {
        const label=document.createElement('label');const check=document.createElement('input');check.type='checkbox';check.checked=selected.has(id);
        check.onchange=()=>check.checked?selected.add(id):selected.delete(id);
        label.append(check,document.createTextNode(room.title));row.append(label);
      }
      row.append(button('保存授权范围',()=>action({action:'grant',user,rooms:[...selected]})),button('撤销授权',()=>action({action:'revoke',user})));grants.append(row);
    }
    window.reboPageReady = true;
    clearTimeout(window.reboInitTimer);
    el('controls').disabled = false;
    el('page-state').textContent = '✅ 页面已就绪 · WebUI 授权连接正常';
    el('reload-page').hidden = true;
  } catch(e) {el('account').textContent='⚠️ 状态读取失败，不能确认当前登录状态';fail(e);}
}
el('login').onclick=async()=>{const password=el('password').value;el('password').value='';await action({action:'login',phone:el('phone').value,password,remember:el('remember').checked});};
for(const name of ['relogin','logout','refresh'])el(name).onclick=()=>action({action:name});
el('status').onclick=status;
await status();
setInterval(() => {if (window.reboPageReady && !busy && !document.hidden) status();}, 15000);
