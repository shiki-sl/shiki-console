const $ = (s, root = document) => root.querySelector(s);
const main = $('#main');
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const finite = n => typeof n === 'number' && Number.isFinite(n);
const pct = n => finite(n) ? `${Math.round(n)}` : '—';
const bytes = n => !finite(n) ? 'N/A' : n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(1)} GiB` : `${Math.round(n / 1024 ** 2)} MiB`;
const date = t => finite(t) && t > 0 ? new Date(t * 1000).toLocaleString('zh-CN', {hour12:false}) : '未提供';
const ageText = s => !finite(s) ? '时间未知' : s < 60 ? `${Math.floor(s)} 秒前` : s < 3600 ? `${Math.floor(s / 60)} 分钟前` : s < 86400 ? `${Math.floor(s / 3600)} 小时前` : `${Math.floor(s / 86400)} 天前`;
const statusLabel = s => ({ok:'正常',error:'异常',unknown:'未知',not_installed:'未安装',running:'运行中',idle:'空闲',online:'在线',offline:'离线',stale:'已过期',unregistered:'未注册',current:'已是最新',up_to_date:'已是最新',available:'有更新',pending:'待更新',success:'成功',failed:'失败',unmanaged:'不托管',unsupported:'不支持',not_configured:'未配置',checking:'检查中',planned:'规划中',deferred:'等待空闲',attention:'需关注'}[s] || s || '未知');
const tone = s => ['ok','online','current','up_to_date','success','running'].includes(s) ? 'ok' : ['error','offline','failed'].includes(s) ? 'bad' : ['stale','available','pending','warning'].includes(s) ? 'warning' : 'unknown';
let config;
try { config = JSON.parse(localStorage.getItem('shiki.preferences') || '{}'); } catch { config = {}; }
config = {primary:location.origin, backup:'', interval:15, ...config};
let data = null, view = location.hash.slice(1) || 'overview', filter = 'all', busy = false;
let clock = {server:Date.now() / 1000, perf:performance.now()}, loadedAt = null, networkError = '', degraded = false, cached = false, timer;
const retained = new Map();
let detailDevice = null;
const now = () => clock.server + (performance.now() - clock.perf) / 1000;
const tokenKey = base => `shiki.viewer:${new URL(base).origin}`;
// Migrate an existing session once; a later clear must not revive keys from an older tab.
try {
  const migrate = localStorage.getItem('shiki.viewer.storage') !== 'persistent-v1';
  for (const key of Object.keys(sessionStorage).filter(key => key.startsWith('shiki.viewer:'))) {
    if (migrate && !localStorage.getItem(key)) localStorage.setItem(key, sessionStorage.getItem(key));
    sessionStorage.removeItem(key);
  }
  localStorage.setItem('shiki.viewer.storage', 'persistent-v1');
} catch { /* Saving below reports unavailable persistent storage to the user. */ }
const getToken = base => { try { return localStorage.getItem(tokenKey(base)) || ''; } catch { return ''; } };
function saveToken(base, token) {
  try { localStorage.setItem(tokenKey(base), token); }
  catch { throw Error('无法在本机保存查看密钥，请检查浏览器的存储权限。'); }
}
function clearViewerTokens() {
  for (const key of Object.keys(localStorage).filter(key => key.startsWith('shiki.viewer:'))) localStorage.removeItem(key);
  localStorage.setItem('shiki.viewer.storage', 'persistent-v1');
  try {
    for (const key of Object.keys(sessionStorage).filter(key => key.startsWith('shiki.viewer:'))) sessionStorage.removeItem(key);
  } catch { /* The migration marker prevents old session keys from being imported again. */ }
}
const persistPrefs = () => {
  try { localStorage.setItem('shiki.preferences', JSON.stringify(config)); }
  catch { throw Error('无法在本机保存接收端设置，请检查浏览器的存储权限。'); }
};
function safeReceiver(raw, allowEmpty = false) {
  if (!raw && allowEmpty) return '';
  const u = new URL(raw);
  const loopback = ['localhost','127.0.0.1','[::1]'].includes(u.hostname);
  if (u.username || u.password || u.search || u.hash || !['http:','https:'].includes(u.protocol) || (u.protocol === 'http:' && !loopback)) throw Error('接收端需使用 HTTPS；本机回环地址可使用 HTTP。不得包含密钥、查询参数或用户名。');
  if (u.pathname !== '/' && u.pathname !== '') throw Error('请输入接收端根地址，例如 https://console.example.com');
  return u.origin;
}
function deviceAge(d) {
  const t = finite(d.sampled_at) ? d.sampled_at : d.received_at;
  return finite(t) && t > 0 ? Math.max(0, now() - t) : Infinity;
}
function deviceState(d) {
  if (!d.registered || !d.snapshot) return 'unregistered';
  if (cached || !navigator.onLine || networkError) return 'stale';
  const age = deviceAge(d);
  if (!finite(age)) return 'unknown';
  const fresh = data?.policy?.fresh_seconds ?? 90, offline = data?.policy?.offline_seconds ?? 300;
  if (d.connection === 'offline' || age > offline) return 'offline';
  if (d.connection === 'stale' || age > fresh) return 'stale';
  return d.connection === 'online' ? 'online' : 'unknown';
}
function unavailable(d) { return deviceState(d) !== 'online' || !!networkError || degraded || d._retained; }
function serviceFresh(d, service, name) {
  if (unavailable(d) || !finite(service?.checked_at) || service.checked_at <= 0) return false;
  const age = now() - service.checked_at;
  return age >= 0 && age <= (name === 'updates' ? (data?.policy?.update_fresh_seconds ?? 172800) : (data?.policy?.fresh_seconds ?? 90));
}
function serviceText(d, service, name) {
  const label = serviceFresh(d, service, name) ? statusLabel(service.status) : `未知 / 未确认（上次：${statusLabel(service.status)}）`;
  return `${label} · ${service.detail || '无补充说明'} · ${date(service.checked_at)}`;
}
function serviceRows(d) {
  return Object.entries(d.snapshot?.services || {}).map(([name, service]) => row(name, serviceText(d, service, name))).join('') || row('检查结果','N/A · 尚未采集');
}
function refreshOpenDetail() {
  if (!$('#detail').open) return;
  const d = data?.devices.find(d => d.id === $('#detail').dataset.device);
  if (!d) return;
  const scroll = $('#detail').scrollTop;
  detail(d, $('#detail').dataset.sort || 'cpu');
  $('#detail').scrollTop = scroll;
}
function issues() {
  return (data?.devices || []).flatMap(d => {
    const state = deviceState(d);
    const connectionIssue = d.expected_online && ['offline','stale'].includes(state);
    const list = (d.issues || []).filter(i => i.severity !== 'unknown' && !(connectionIssue && i.component === 'connection')).map(i => ({...i, name:d.name, deviceId:d.id}));
    if (connectionIssue) list.unshift({name:d.name, deviceId:d.id, component:'采集连接',severity:state === 'offline' ? 'error' : 'warning',message:`${statusLabel(state)} · 最近采样 ${ageText(deviceAge(d))}，历史数值仅供参考`});
    return list;
  }).sort((a,b) => (a.severity === 'error' ? 0 : 1) - (b.severity === 'error' ? 0 : 1));
}
function headline() {
  if (!navigator.onLine) return {en:'PHONE OFFLINE', title:'手机当前<em>离线。</em>',text:'显示已保存的历史采样。恢复网络后可刷新。',class:'unknown'};
  if (networkError) return {en:networkError.includes('401') ? 'VIEWER KEY REQUIRED' : 'RECEIVER UNREACHABLE',title:networkError.includes('401') ? '查看密钥<em>需要检查。</em>' : '接收端<em>暂不可达。</em>',text:networkError,class:'warning'};
  if (degraded) return {en:'DEGRADED · STANDBY VIEW',title:'备用接收端<em>正在接力。</em>',text:'主接收端失败；当前为降级视图，每次刷新都会先重试主接收端。',class:'warning'};
  if (!data) return {en:'AWAITING CONNECTION',title:'连接你的<em>数字空间。</em>',text:'配对接收端，开始查看设备与服务状态。',class:'unknown'};
  const ds = data.devices || [], attention = new Set(issues().map(i => i.deviceId)).size;
  if (attention) return {en:`${attention} SYSTEMS NEED ATTENTION`,title:`${attention} 台设备<em>需要关注。</em>`,text:'先处理下面的异常，再继续查看实时观测。',class:'warning'};
  const unknown = ds.filter(d => d.expected_online && (deviceState(d) !== 'online' || d.health !== 'ok' || d._retained || (d.required_services || []).some(name => !serviceFresh(d, d.snapshot?.services?.[name], name)))).length;
  if (cached || !ds.some(d=>d.expected_online) || unknown || data.summary?.state !== 'ok' || data.summary?.unknown_count > 0) return {en:'COVERAGE UNKNOWN',title:'观测覆盖<em>尚未完整。</em>',text:'未注册、未上报或未知项不能视为正常。',class:'unknown'};
  return {en:'ALL SYSTEMS OPERATIONAL',title:'一切就绪，<em>尽在掌握。</em>',text:'所有已配置设备与检查项均已上报正常。',class:'ok'};
}
function hero() {
  const h = headline();
  return `<section class="hero"><div class="hero-status ${h.class}"><i class="dot"></i>${esc(h.en)}</div><h1>${h.title}</h1><p>${esc(h.text)}</p><p class="freshness">${loadedAt ? `视图更新 ${esc(ageText(Math.max(0,now()-loadedAt)))} · ${esc(date(loadedAt))}` : '尚无接收端数据'}${cached ? ' · 本地历史快照（最长保留 24 小时）' : ''}</p></section>`;
}
function metric(label, value, unit, percent, detail, cls = '') {
  if (value === '—' || value === 'N/A') { value = 'N/A'; unit = ''; }
  return `<div class="metric ${cls}"><label>${esc(label)}</label><strong>${esc(value)}<small>${esc(unit)}</small></strong>${finite(percent) ? `<progress max="100" value="${Math.max(0,Math.min(100,percent))}" aria-label="${esc(label)} ${esc(pct(percent))}%"></progress>` : '<progress max="100" value="0" aria-label="没有可用数据"></progress>'}<span class="sub">${esc(detail || '未采集到数据')}</span></div>`;
}
const desktopIcon = '<svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 21h8M12 16v5"/></svg>';
function card(d) {
  const hw = d.snapshot?.hardware || {}, cpu = hw.cpu || {}, mem = hw.memory || {}, disks = hw.disks || [], state = deviceState(d);
  const mempct = finite(mem.used_bytes) && mem.total_bytes > 0 ? mem.used_bytes / mem.total_bytes * 100 : null;
  const gpus = hw.gpus || [];
  const services = Object.entries(d.snapshot?.services || {});
  const gpuRows = `<div class="gpu-list">${gpus.length ? gpus.slice(0,2).map(gpu=>`<div class="gpu-row"><span class="gpu-name">${esc(gpu.name || 'GPU')}</span><strong>${finite(gpu.usage_percent) ? esc(pct(gpu.usage_percent))+'%' : 'N/A'}<small>使用率</small></strong><strong>${finite(gpu.temperature_c) ? esc(gpu.temperature_c.toFixed(0))+'°C' : 'N/A'}<small>温度</small></strong>${(!finite(gpu.usage_percent) || !finite(gpu.temperature_c)) ? `<span class="gpu-reason">${esc(gpu.error || '采集源未提供完整 GPU 指标')}</span>` : ''}</div>`).join('') : '<div class="gpu-reason">GPU · N/A — 无可用 GPU 采样</div>'}${gpus.length > 2 ? `<div class="gpu-reason">另有 ${gpus.length-2} 个 GPU，查看设备详情</div>` : ''}</div>`;
  return `<article class="card ${unavailable(d) ? 'stale' : ''}"><div class="card-head"><div class="device-name"><div class="device-icon">${desktopIcon}</div><div><h3>${esc(d.name)}</h3><small>${esc(d.site || '未分组')} · ${d.mobility === 'mobile' ? '移动设备' : '固定设备'}</small></div></div><span class="badge ${tone(state)}"><i class="dot"></i>${esc(statusLabel(state))}</span></div><div class="metrics primary-metrics">${metric('CPU',pct(cpu.usage_percent),'%',cpu.usage_percent, finite(cpu.temperature_c) ? `${cpu.temperature_c.toFixed(0)}°C · ${cpu.name || 'CPU'}` : cpu.error || cpu.name)}${metric('MEMORY',finite(mempct) ? pct(mempct) : '—','%',mempct, finite(mem.used_bytes) ? `${bytes(mem.used_bytes)} / ${bytes(mem.total_bytes)}` : mem.error)}</div>${gpuRows}${disks.length ? `<div class="freshness">${disks.map(disk => `${esc(disk.mount)} · ${esc(bytes(disk.used_bytes))} / ${esc(bytes(disk.total_bytes))}`).join(' &nbsp; · &nbsp; ')}</div>` : ''}<div class="card-bottom"><div class="service-dots">${services.length ? services.map(([name,s]) => `<span><i class="dot ${serviceFresh(d,s,name) ? tone(s.status) : 'unknown'}"></i>${esc(name)}</span>`).join('') : '<span>服务状态待采集</span>'}</div><button class="text-button" data-device="${esc(d.id)}">设备详情 ↗</button></div><div class="freshness">${esc(date(d.sampled_at))} · ${esc(ageText(deviceAge(d)))}${d._retained ? ` · 保留 ${esc(d._source)} 的较新采样` : ''}${unavailable(d) ? ' · 历史 / 未确认' : ''}</div></article>`;
}
function filteredDevices() { return (data?.devices || []).filter(d => filter === 'all' || d.site === filter); }
function filters() {
  const sites = [...new Set((data?.devices || []).map(d=>d.site).filter(Boolean))];
  return `<div class="filters" aria-label="按位置筛选"><button class="chip ${filter === 'all' ? 'active' : ''}" data-filter="all">全部设备</button>${sites.map(s => `<button class="chip ${filter === s ? 'active' : ''}" data-filter="${esc(s)}">${esc(s)}</button>`).join('')}</div>`;
}
function pairCard() { return `<section class="card pair-card"><span class="eyebrow">PRIVATE BY DEFAULT</span><h2>与你的接收端配对</h2><p>从接收端的 <code>viewer.key</code> 复制只读查看密钥。此处仅登录查看，不注册或添加设备。密钥保存在本机，关闭后重开仍可查看；可在「设置」中清除本机配对。</p><form id="pair-form"><label class="sr-only" for="pair-token">查看密钥</label><input id="pair-token" type="password" name="token" autocomplete="off" placeholder="粘贴 viewer key" required><button class="button" type="submit">连接接收端 ↗</button></form><p class="form-note">当前接收端：${esc(config.primary)}<br>其他接收端可在「设置」中单独配对。</p></section>`; }
function containerSummary(devices) {
  const known = devices.filter(d=>finite(d.snapshot?.docker?.running) && d.snapshot.docker.running >= 0 && !d.snapshot.docker.error && d.snapshot.docker.status !== 'unknown');
  return {value:known.length ? known.reduce((sum,d)=>sum+d.snapshot.docker.running,0).toString().padStart(2,'0') : 'N/A', coverage:known.length && known.length < devices.length ? ` · 部分 ${known.length}/${devices.length} 台已知` : '', historical:known.some(unavailable)};
}
function renderOverview() {
  const ds = data?.devices || [], online = ds.filter(d=>deviceState(d) === 'online').length;
  const alerts = issues();
  const services = ds.flatMap(d=>Object.entries(d.snapshot?.services || {}).map(([name,s])=>({d,s,name})));
  const serviceOk = services.filter(({d,s,name})=>serviceFresh(d,s,name) && s.status === 'ok').length;
  const containers = containerSummary(ds);
  return `${hero()}<div class="summary"><div><strong>${online.toString().padStart(2,'0')}</strong><small>/ ${ds.length.toString().padStart(2,'0')}</small><span>在线设备</span></div><div><strong>${serviceOk.toString().padStart(2,'0')}</strong><small>/ ${services.length}</small><span>确认正常服务</span></div><div><strong>${containers.value}</strong><span>上报运行容器${containers.coverage}${containers.historical ? ' · 历史' : ''}</span></div></div>${!getToken(config.primary) ? pairCard() : ''}${alerts.length ? `<div class="section-heading"><h2>优先关注</h2><span class="count">${alerts.length} EVENTS</span></div>${alerts.slice(0,5).map(i=>`<div class="alert ${i.severity === 'error' ? 'error' : ''}"><span class="symbol">!</span><div><strong>${esc(i.name)} · ${esc(i.component)}</strong><p>${esc(i.message)}</p></div></div>`).join('')}` : ''}<div class="section-heading"><h2>设备概览</h2><span class="count">${ds.length} DEVICES</span></div>${filters()}<div class="device-grid">${filteredDevices().map(card).join('') || '<div class="empty">设备上报后，采样数据会显示在这里。<br>不会生成演示指标或估算趋势。</div>'}</div><div class="section-heading"><h2>连接与更新</h2><span class="count">OBSERVABILITY</span></div><div class="mini-grid"><article class="card mini"><span class="eyebrow">RECEIVER</span><strong>${esc(data?.receiver?.name || '待连接')}</strong><p>${esc(data?.receiver?.site || '接收端位置未知')}<br>${degraded ? '备用视图 · 降级' : cached ? '本地历史快照' : networkError ? '连接失败' : loadedAt ? '已获取只读数据' : '尚未配对'}</p></article><article class="card mini"><span class="eyebrow">UPDATE POLICY</span><strong>${ds.filter(d=>d.update_managed).length} <small>托管设备</small></strong><p>系统 / CLI / GUI 分别观测<br>移动设备可标记为不托管</p></article></div>`;
}
function renderApps() {
  const groups = (data?.devices || []).map(d=>({d,apps:(d.apps || []).filter(a=>a.enabled && safeApp(a.url))})).filter(g=>g.apps.length);
  return `<h1 class="page-title">你的应用，<br>随时抵达。</h1><p class="subtitle">配置好的服务入口。直接在浏览器打开；连接范围由应用自身决定。</p>${groups.map(({d,apps})=>`<div class="section-heading"><h2>${esc(d.name)}</h2><span class="count">${esc(d.site)}</span></div><div class="device-grid">${apps.map(a=>`<a class="card app-link" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer"><span class="app-letter">${esc(a.name.slice(0,1).toUpperCase())}</span><div><h3>${esc(a.name)}</h3><p>${esc(a.description || '')}<br>${esc(({lan:'局域网',tailnet:'Tailnet',public:'公网'})[a.access] || '访问范围未知')}</p></div><span class="arrow">↗</span></a>`).join('')}</div>`).join('') || '<div class="empty">暂无配置的应用入口。<br>应用由接收端配置提供。</div>'}`;
}
function safeApp(url) { try { const u = new URL(url); return ['http:','https:'].includes(u.protocol) && !u.username && !u.password; } catch { return false; } }
function renderSettings() {
  return `<h1 class="page-title">观测偏好</h1><p class="subtitle">这里只配置本机查看方式，不会更改设备或接收端。</p><form id="settings-form"><section class="card form-card"><h2>接收端与查看密钥</h2><label class="field"><span>主接收端 · PRIMARY</span><input name="primary" type="url" value="${esc(config.primary)}" required autocomplete="off"></label><label class="field"><span>主接收端查看密钥 · 留空保留当前密钥</span><input name="primaryToken" type="password" autocomplete="off" placeholder="${getToken(config.primary) ? '本机已保存密钥' : '粘贴 viewer.key'}"></label><label class="field"><span>备用接收端 · 默认未启用</span><input name="backup" type="url" value="${esc(config.backup)}" autocomplete="off" placeholder="https://standby.example.com"></label><label class="field"><span>备用接收端独立查看密钥</span><input name="backupToken" type="password" autocomplete="off" placeholder="${config.backup && getToken(config.backup) ? '本机已保存密钥' : '仅配置 URL 不会启用故障切换'}"></label><p class="form-note">主端请求失败后，最多等待 3 秒，再顺序尝试已配对的备用端。备用端数据有独立来源与时间；较旧采样不会覆盖较新采样。跨域接收端需配置允许本页面来源的 CORS。</p></section><section class="card form-card"><h2>刷新与显示</h2><label class="field"><span>自动刷新 · 页面隐藏时暂停</span><select name="interval">${[5,15,30,60,0].map(n=>`<option value="${n}" ${Number(config.interval) === n ? 'selected' : ''}>${n ? `每 ${n} 秒` : '关闭，仅手动刷新'}</option>`).join('')}</select></label><p class="form-note">支持下拉刷新。数据按接收端的时效策略标记过期 / 离线（默认 90 / 300 秒）；采集端给出的更差状态会保留。PWA 后台刷新和常驻连接不作保证。</p><div class="form-actions"><button class="button" type="submit">保存并刷新</button><button class="button secondary" id="forget" type="button">清除本机配对</button></div></section></form><section class="card form-card"><h2>本地隐私</h2><p class="form-note">查看密钥保存在当前浏览器或 PWA 的本机存储中，关闭后保留；「清除本机配对」会删除已保存的密钥。离线快照仅保留设备指标及检查状态，最长 24 小时，不包含密钥、任务内容或应用入口。所有数据 API 均不进入 Service Worker 缓存。</p><div class="form-actions"><button id="clear-cache" class="button secondary">清除历史快照</button></div></section>`;
}
function render() {
  if (!['overview','devices','apps','settings'].includes(view)) view='overview';
  // Preserve form focus and unsaved fields across freshness ticks.
  if (view === 'settings' && $('#settings-form')) return;
  if ($('#pair-token')?.value || document.activeElement === $('#pair-token')) return;
  main.innerHTML = view === 'overview' ? renderOverview() : view === 'devices' ? `${hero()}${filters()}<div class="device-grid">${filteredDevices().map(card).join('') || '<div class="empty">没有可显示的设备。</div>'}</div>` : view === 'apps' ? renderApps() : renderSettings();
  document.querySelectorAll('[data-tab]').forEach(a=>{a.classList.toggle('active',a.dataset.tab===view);a.setAttribute('aria-current',a.dataset.tab === view ? 'page' : 'false');});
}
function row(label,value) { return `<div class="data-row"><span>${esc(label)}</span><strong>${esc(value ?? 'N/A · 未采集')}</strong></div>`; }
function detail(d, sort = 'cpu') {
  detailDevice = d;
  $('#detail').dataset.device=d.id;
  $('#detail').dataset.sort=sort;
  const s = d.snapshot || {}, c = s.codex || {}, docker = s.docker || {}, h = s.hardware || {};
  $('#detail-body').innerHTML = `<h2 class="page-title">${esc(d.name)}</h2><p id="detail-freshness" class="subtitle">${esc(d.site)} · ${esc(statusLabel(deviceState(d)))}<br>采样 ${esc(date(d.sampled_at))} · ${esc(ageText(deviceAge(d)))}${unavailable(d) ? '<br>以下为历史 / 未确认数据，不代表当前运行状态。' : ''}</p><section class="detail-section"><h3>硬件采样</h3><div class="data-list">${row('CPU',h.cpu?.name)}${row('CPU 温度',finite(h.cpu?.temperature_c) ? `${h.cpu.temperature_c}°C` : h.cpu?.error)}${(h.gpus || []).map(g=>row(g.name || 'GPU',`${finite(g.usage_percent) ? `${pct(g.usage_percent)}%` : 'N/A'} · 温度 ${finite(g.temperature_c) ? `${g.temperature_c.toFixed(0)}°C` : 'N/A'} · 显存 ${bytes(g.memory_used_bytes)} / ${bytes(g.memory_total_bytes)}${g.error ? ` · ${g.error}` : ''}`)).join('')}${(h.disks || []).map(disk=>row(`磁盘 ${disk.mount}`,`${bytes(disk.used_bytes)} / ${bytes(disk.total_bytes)}${disk.error ? ` · ${disk.error}` : ''}`)).join('')}</div></section><section class="detail-section"><h3>服务检查</h3><div id="detail-services" class="data-list">${serviceRows(d)}</div></section><section class="detail-section"><h3>Codex · 数据来源明确可见</h3><p>进程存在仅表示进程检测结果；任务、模型和速度仅在采集源明确提供时展示，不能由 CPU 使用率推断。</p>${row('GUI 进程',c.gui_running == null ? '未知 · 无检测结果' : c.gui_running ? '检测到进程' : '未检测到进程')}${row('CLI 进程',c.cli_running == null ? '未知 · 无检测结果' : c.cli_running ? '检测到进程' : '未检测到进程')}${row('模型',c.model || 'N/A · 未提供模型来源')}${row('任务状态',c.task_state ? statusLabel(c.task_state) : 'N/A · 未提供任务来源')}${row('最近任务',c.last_task_label || 'N/A · 无任务事件')}${row('最近任务时间',date(c.last_task_at))}${row('实时 / 平均速度',`${finite(c.realtime_tps) ? c.realtime_tps+' t/s' : 'N/A'} / ${finite(c.average_tps) ? c.average_tps+' t/s' : 'N/A'}`)}${row('采集说明',c.error || '仅展示采集端明确上报的字段')}</section><section class="detail-section"><h3>Docker · ${esc(docker.running ?? '—')} 运行 / ${esc(docker.total ?? '—')} 总计</h3><div class="filters"><button class="chip ${sort === 'cpu' ? 'active' : ''}" data-sort="cpu">按 CPU</button><button class="chip ${sort === 'memory' ? 'active' : ''}" data-sort="memory">按内存</button></div><p>${esc(docker.error || `状态：${statusLabel(docker.status)} · 已退出 ${docker.exited ?? 'N/A'}`)}</p>${[...(docker.containers || [])].sort((a,b)=> sort === 'memory' ? (b.memory_bytes || 0)-(a.memory_bytes || 0) : (b.cpu_percent || 0)-(a.cpu_percent || 0)).map(k=>`<article class="container"><b>${esc(k.name)}</b><p>${esc(k.state)} · health ${esc(k.health || 'N/A')}${k.error ? ` · ${esc(k.error)}` : ''}</p><div class="container-stats"><span>CPU ${finite(k.cpu_percent) ? esc(k.cpu_percent)+'%' : 'N/A'}</span><span>内存 ${esc(bytes(k.memory_bytes))}</span></div></article>`).join('') || '<p>无容器明细。容器按 CPU 使用率排列。</p>'}</section><section class="detail-section"><h3>网络 · Tailscale</h3>${row('状态',statusLabel(s.tailscale?.status))}${row('在线节点',s.tailscale?.online_peers)}${(s.tailscale?.peers || []).map(p=>row(p.name,p.online ? '在线' : '离线')).join('')}${s.tailscale?.error ? row('说明',s.tailscale.error) : ''}</section><section class="detail-section"><h3>更新状态 · ${d.update_managed ? '托管' : '不托管'}</h3>${['system','cli','gui'].map(key=>{const u=s.updates?.[key];return row(({system:'系统',cli:'Codex CLI',gui:'Codex GUI'})[key], u ? `${statusLabel(u.state)} · ${u.version || '版本未知'} · 最近检查 ${date(u.checked_at)} · 最近成功 ${date(u.last_success_at)}${u.detail ? ` · ${u.detail}` : ''}` : 'N/A · 无更新检查结果');}).join('')}${row('需要重启',s.updates?.reboot_required == null ? '未知' : s.updates.reboot_required ? '是' : '否')}</section><section class="detail-section"><h3>上报链路</h3>${(s.delivery || []).map(v=>row(v.receiver_id,`${statusLabel(v.status)} · 最近成功 ${date(v.last_success_at)}${v.error ? ` · ${v.error}` : ''}`)).join('') || row('接收来源',d._source || data?.receiver?.id || '未知')}</section>`;
  if(!$('#detail').open) $('#detail').showModal();
}
function mergeData(payload) {
  if (payload.schema !== 1 || !Array.isArray(payload.devices) || !finite(payload.receiver?.server_time)) throw Error('接收端返回了不兼容的数据格式');
  const source = payload.receiver.id;
  const newClock = {server:payload.receiver.server_time,perf:performance.now()};
  const ds = payload.devices.map(d=> {
    const previous = retained.get(d.id);
    const oldTime = previous?.sampled_at || 0, newTime = d.sampled_at || 0;
    if (previous && previous.snapshot && oldTime > newTime) return {...d,...previous,apps:d.apps,_retained:true};
    const next = {...d,_source:source,_retained:false};
    retained.set(d.id,next);return next;
  });
  clock = newClock; loadedAt=payload.receiver.server_time;
  data={...payload,devices:ds};cached=false;cacheSnapshot();
}
function cacheSnapshot() {
  const devices = data.devices.map(d=>({id:d.id,name:d.name,site:d.site,mobility:d.mobility,expected_online:d.expected_online,update_managed:d.update_managed,registered:d.registered,connection:'stale',sampled_at:d.sampled_at,received_at:d.received_at,health:'unknown',issues:[],_source:d._source,snapshot:d.snapshot ? {hardware:d.snapshot.hardware,services:d.snapshot.services,docker:d.snapshot.docker,tailscale:d.snapshot.tailscale,updates:d.snapshot.updates} : null}));
  try { localStorage.setItem('shiki.lastmetrics',JSON.stringify({saved:Date.now(),loadedAt,receiver:data.receiver,devices})); } catch { /* a full storage must not break live monitoring */ }
}
function restoreSnapshot() {
  try {
    const v=JSON.parse(localStorage.getItem('shiki.lastmetrics') || 'null');
    if (!v) return;
    if (Date.now()-v.saved > 86400000 || Date.now() < v.saved) {localStorage.removeItem('shiki.lastmetrics');return;}
    data={schema:1,receiver:v.receiver,devices:v.devices,summary:{state:'unknown'}};loadedAt=v.loadedAt;cached=true;
    v.devices.forEach(d=>retained.set(d.id,d));
  } catch { localStorage.removeItem('shiki.lastmetrics'); }
}
async function request(base) {
  const token=getToken(base);
  if (!token) throw Error('401 · 此接收端尚未配对查看密钥');
  const controller = new AbortController(), timeout=setTimeout(()=>controller.abort(),3000);
  try {
    const res=await fetch(`${base}/api/v1/dashboard`,{headers:{Authorization:`Bearer ${token}`},signal:controller.signal,cache:'no-store',credentials:'omit',redirect:'error'});
    if (!res.ok) throw Error(res.status === 401 ? '401 · 查看密钥无效或已撤销' : `接收端返回 HTTP ${res.status}`);
    const payload=await res.json();
    if (payload.schema !== 1 || !Array.isArray(payload.devices) || !finite(payload.receiver?.server_time)) throw Error('接收端数据格式无效');
    return payload;
  } catch (e) { throw Error(e.name === 'AbortError' ? '接收端在 3 秒内未响应' : e.message); }
  finally { clearTimeout(timeout); }
}
async function refresh() {
  if (busy || document.hidden) return;
  busy=true;$('#refresh').disabled=true;
  try {
    if (!navigator.onLine) throw Error('手机离线；已暂停请求');
    try {
      const payload=await request(config.primary);mergeData(payload);degraded=false;networkError='';
      const expected=payload.devices.filter(d=>d.expected_online && d.registered);
      if(expected.length && expected.every(d=>!finite(d.sampled_at) || payload.receiver.server_time-d.sampled_at>(payload.policy?.fresh_seconds ?? 90)) && config.backup && getToken(config.backup)) throw Error('主接收端的预期在线设备采样均已过期');
    }
    catch(primaryError) {
      if(config.backup && getToken(config.backup) && config.backup !== config.primary) {
        try {const payload=await request(config.backup);mergeData(payload);degraded=true;networkError='';}
        catch(backupError) {throw Error(`主端：${primaryError.message}；备用：${backupError.message}`);}
      } else throw primaryError;
    }
  } catch(e) {networkError=e.message;}
  finally {busy=false;$('#refresh').disabled=false;render();refreshOpenDetail();schedule();}
}
function schedule() { clearTimeout(timer);if(Number(config.interval)>0 && !document.hidden) timer=setTimeout(refresh,Number(config.interval)*1000); }
function toast(text) { const t=$('#toast');t.textContent=text;t.hidden=false;setTimeout(()=>t.hidden=true,3000); }
$('#refresh').addEventListener('click',refresh);
$('#close-detail').addEventListener('click',()=>$('#detail').close());
$('#detail').addEventListener('click',e=>{if(e.target===$('#detail')) $('#detail').close();const sort=e.target.closest('[data-sort]');if(sort){const d=data?.devices.find(d=>d.id===$('#detail').dataset.device);if(d)detail(d,sort.dataset.sort);}});
window.addEventListener('hashchange',()=>{view=location.hash.slice(1)||'overview';main.innerHTML='';render();window.scrollTo(0,0);});
main.addEventListener('click',e=>{
  const chip=e.target.closest('[data-filter]');if(chip){filter=chip.dataset.filter;render();}
  const device=e.target.closest('[data-device]');if(device){const d=data?.devices.find(d=>d.id===device.dataset.device);if(d)detail(d);}
  if(e.target.closest('#forget')){try{clearViewerTokens();networkError='查看密钥已从本机清除';main.innerHTML='';render();toast('本机配对已清除');}catch{toast('无法清除查看密钥，请检查浏览器的存储权限。');}}
  if(e.target.closest('#clear-cache')){localStorage.removeItem('shiki.lastmetrics');if(cached){data=null;retained.clear();loadedAt=null;}toast('历史快照已清除');}
});
main.addEventListener('submit',e=>{
  e.preventDefault();
  if(e.target.id==='pair-form'){try{const token=new FormData(e.target).get('token').trim();saveToken(config.primary,token);e.target.reset();refresh().then(()=>{if(!networkError){view='overview';location.hash='overview';main.innerHTML='';render();window.scrollTo(0,0);}});}catch(err){toast(err.message);}}
  if(e.target.id==='settings-form'){
    try {
      const f=new FormData(e.target),primary=safeReceiver(f.get('primary').trim()),backup=safeReceiver(f.get('backup').trim(),true);
      if(backup && primary===backup) throw Error('主接收端与备用接收端需为不同来源');
      const previous=config.primary;
      config={primary,backup,interval:Number(f.get('interval'))};
      for(const [base,key] of [[primary,'primaryToken'],[backup,'backupToken']]) if(base && f.get(key).trim()) saveToken(base,f.get(key).trim());
      persistPrefs();if(previous!==primary){data=null;loadedAt=null;retained.clear();localStorage.removeItem('shiki.lastmetrics');}
      toast('查看偏好已保存');main.innerHTML='';render();refresh();
    }catch(err){toast(err.message);}
  }
});
document.addEventListener('visibilitychange',()=>{if(document.hidden)clearTimeout(timer);else{render();refresh();}});
window.addEventListener('online',refresh);window.addEventListener('offline',()=>{networkError='手机离线';render();});
let touchStart=null;
document.addEventListener('touchstart',e=>{if(window.scrollY===0 && !$('#detail').open && view!=='settings')touchStart=e.touches[0].clientY;else touchStart=null;},{passive:true});
document.addEventListener('touchend',e=>{if(touchStart!==null && e.changedTouches[0].clientY-touchStart>90){refresh();toast('正在刷新观测数据');}touchStart=null;},{passive:true});
setInterval(()=>{
  if(document.hidden)return;
  if($('#detail').open){
    const d=detailDevice;
    if(d && $('#detail-services')) $('#detail-services').innerHTML=serviceRows(d);
    if(d && $('#detail-freshness')) $('#detail-freshness').textContent=`${d.site || '未分组'} · ${statusLabel(deviceState(d))} · 采样 ${date(d.sampled_at)} · ${ageText(deviceAge(d))}${unavailable(d) ? ' · 历史 / 未确认数据，不代表当前运行状态' : ''}`;
  }else if(!['settings','apps'].includes(view) && !$('#pair-token:focus'))render();
},1000);
restoreSnapshot();render();refresh();
if('serviceWorker' in navigator && window.isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{});
