const $ = id => document.getElementById(id);
const methods = ['电话', '当面', '室友转达'];
let revision = '', schoolRevision = '', schoolReady = false, schoolVerified = false;
let schoolRecords = [], draftRevision = '', drafts = new Map();
let batchToken = '', batchGeneration = 0;
let previewPreparing = false, schoolBusy = false;
let localSubmitting = false, serverRunning = false, statusBusy = false;
let lastBatchStatus = '';
let batchExpiry;
let gateOpen = true, gateBusy = false, gateTimer = null;
const gateWaiting = {endpoint_unavailable: '尚未打开学校登录窗口，请点击上方按钮。', login_required: '学校页面已打开，等待登录…',
  loading: '学校页面加载中…', not_school_page: '当前不在学校业务页，请在独立 Edge 窗口中进入学校 H5。',
  mobile_required: '学校要求手机环境，请在独立 Edge 窗口中重试。', loading_failed: '学校页面加载失败，请重试。'};

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? 'POST' : 'GET',
    headers: body ? {'Content-Type': 'application/json'} : {},
    body: body ? JSON.stringify(body) : undefined,
    cache: 'no-store'
  });
  const value = await response.json();
  if (!response.ok) throw Error(value.error || '请求失败');
  return value;
}
function esc(value) {
  return String(value).replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[char]));
}
function submitting() { return localSubmitting || serverRunning; }
function updateControls() {
  const busy = submitting() || schoolBusy;
  for (const id of ['refresh', 'openSchool', 'readSchool', 'openRoster', 'saveTime']) $(id).disabled = busy;
  $('schoolAck').disabled = busy || !schoolReady || !schoolRecords.length;
  $('verifySchool').disabled = busy || !schoolReady || !$('schoolAck').checked;
  $('applyBatch').disabled = busy || !schoolReady || !schoolRecords.length;
  for (const id of ['batchMethod', 'batchNote']) $(id).disabled = busy;
  document.querySelectorAll('.batch-method, .batch-note').forEach(input => input.disabled = busy);
  $('prepareBatch').disabled = busy || previewPreparing || !schoolReady || !schoolVerified || !schoolRecords.length;
  $('batchAck').disabled = busy || !batchToken;
  $('submitBatch').disabled = busy || !batchToken || !$('batchAck').checked;
  $('oneClickSubmit').disabled = busy || previewPreparing || !schoolReady || !schoolRecords.length || !$('schoolAck').checked;
  $('readAllRoster').disabled = busy || allRosterBusy;
}
function clearBatch() {
  batchGeneration++;
  clearTimeout(batchExpiry);
  batchToken = '';
  $('batchPreview').textContent = '';
  $('batchAck').checked = false;
  updateControls();
}
function collectDrafts() {
  return [...$('schoolRows').querySelectorAll('tr')].map(row => ({
    id: row.dataset.id,
    method: row.querySelector('.batch-method').value,
    explanation: row.querySelector('.batch-note').value.trim()
  }));
}
function saveDrafts() {
  for (const record of collectDrafts()) drafts.set(record.id, record);
}
function renderRows() {
  $('schoolRows').innerHTML = schoolRecords.map(record => {
    const draft = drafts.get(record.id);
    return '<tr data-id="' + esc(record.id) + '"><td>' + esc(record.name) + '</td><td>' +
      esc(record.id) + '</td><td>' + esc(record.status) + '</td><td><select class="batch-method">' +
      methods.map(method => '<option' + (draft.method === method ? ' selected' : '') + '>' + method + '</option>').join('') +
      '</select></td><td><textarea class="batch-note" maxlength="100">' + esc(draft.explanation) + '</textarea></td></tr>';
  }).join('');
}
function showSchool(page) {
  const labels = {connected: '已连接学校 H5', login_required: '已连接，等待正常登录',
    mobile_required: '学校要求手机环境', loading_failed: '页面加载失败',
    endpoint_unavailable: '浏览器未连接', not_school_page: '未进入学校业务页', loading: '页面加载中'};
  const ready = page.status === 'connected' && page.roster_ready === true;
  const changed = !ready || schoolRevision !== page.revision;
  $('schoolStatus').textContent = (labels[page.status] || page.status || '状态未知') +
    (page.title ? ' · ' + page.title : '') + (page.business_date ? ' · 名单日期 ' + page.business_date : '') +
    (page.list_detected ? ' · 未打卡 ' + page.pending_count + ' 人' : '') + (page.detail ? ' · ' + page.detail : '');
  $('schoolText').textContent = page.text || page.url || '';
  schoolReady = ready;
  schoolVerified = ready && Boolean(page.verified_at);
  schoolRevision = ready ? page.revision : '';
  schoolRecords = ready ? page.roster : [];
  $('state').textContent = page.verification || labels[page.status] || '学校名单尚未核验';
  $('dot').className = 'dot ' + (ready ? 'good' : '');
  $('schoolVerification').textContent = (page.verification || '') + (page.verified_at ? ' · 核验时间 ' + page.verified_at : '');
  $('schoolTable').hidden = !ready;
  if (ready) {
    if (draftRevision !== page.revision) {
      drafts = new Map(schoolRecords.map(record => [record.id, {
        id: record.id, method: $('batchMethod').value, explanation: $('batchNote').value.trim()
      }]));
      draftRevision = page.revision;
    }
    if (changed || !$('schoolRows').children.length) renderRows();
  } else $('schoolRows').innerHTML = '';
  if (changed) {
    $('schoolAck').checked = false;
  }
  if (changed || !schoolVerified) { clearBatch(); }
  updateControls();
}
function schoolError(error) {
  showSchool({status: 'endpoint_unavailable', detail: error.message, verification: '连接失效，已暂停'});
}
async function schoolAction(path, body) {
  if (submitting() || schoolBusy) return;
  schoolBusy = true;
  updateControls();
  $('schoolStatus').textContent = '正在读取官方学校页面…';
  try { showSchool(await api(path, body)); }
  catch (error) { schoolError(error); }
  finally { schoolBusy = false; updateControls(); }
}
async function readSchool() { return schoolAction('/api/school-page'); }
async function load() {
  try {
    const state = await api('/api/state');
    revision = state.revision;
    $('date').textContent = state.date;
    $('rosterDate').value = state.date;
    $('demoState').textContent = state.verification;
    if (document.activeElement !== $('runAt')) $('runAt').value = state.settings.time;
    $('count').textContent = '(' + state.count + ' 条)';
    $('rows').innerHTML = state.roster.map(record => '<tr><td>' + esc(record.name) + '</td><td>' + esc(record.status) + '</td></tr>').join('');
    $('execution').textContent = '批量提交需先保存核验快照、检查完整预览并点击提交。';
    $('audit').innerHTML = state.audit.length ? state.audit.map(item => '<div class="audit"><span>' +
      esc(item.at) + '</span><span>' + esc(item.count) + ' 条</span><span>' + esc(item.summary) + '</span></div>').join('') : '<p class="muted">暂无记录</p>';
  } catch (error) { $('demoState').textContent = error.message; }
}
function showBatchResult(result) {
  const labels = {idle: '尚未提交批次', running: '正在提交', success: '批次完成',
    verified: '批次完成', partial: '部分完成，已停止', unknown: '结果不明，已停止', stopped: '已停止'};
  let line = (labels[result.status] || result.status) + ' · 已核对成功 ' +
    result.submitted_count + '/' + result.total_count;
  if (result.confirmed_count !== undefined && result.confirmed_count !== null) {
    line += ' · 名单实时核验：' + result.confirmed_count + ' 人已代确认';
  }
  $('batchResult').textContent = line + ' · ' + result.detail;
}
let allRosterBusy = false;
function showAllRoster(data) {
  const records = data.records || [], counts = data.counts || {};
  $('allRosterSummary').textContent = '全班 ' + records.length + ' 人 · 已打卡 ' + (counts['已打卡'] || 0) +
    ' · 已代确认 ' + (counts['已代确认'] || 0) + ' · 未打卡 ' + (counts['未打卡'] || 0);
  $('allRosterRows').innerHTML = records.map(record => {
    const cls = record.status === '已代确认' ? 'st-confirmed' :
      record.status === '未打卡' ? 'st-pending' : 'st-done';
    return '<tr><td>' + esc(record.name) + '</td><td>' + esc(record.id) + '</td><td><span class="st ' +
      cls + '">' + esc(record.status) + '</span></td></tr>';
  }).join('');
  $('allRosterTable').hidden = !records.length;
}
async function readAllRoster() {
  if (allRosterBusy || submitting() || schoolBusy) return;
  allRosterBusy = true;
  updateControls();
  $('allRosterResult').textContent = '正在读取官方「全部」看板…';
  try {
    const data = await api('/api/all-roster', {});
    showAllRoster(data);
    const counts = data.counts || {};
    $('allRosterResult').textContent = '已读取官方看板实时状态（' + data.date + '）· 已代确认 ' +
      (counts['已代确认'] || 0) + ' 人' + (counts['未打卡'] ? ' · 仍有 ' + counts['未打卡'] + ' 人未打卡' : '');
  } catch (error) { $('allRosterResult').textContent = error.message; }
  finally { allRosterBusy = false; updateControls(); }
}
$('readAllRoster').onclick = readAllRoster;
async function readBatchStatus() {
  if (statusBusy) return;
  statusBusy = true;
  try {
    const status = await api('/api/batch-status');
    serverRunning = status.status === 'running';
    const signature = JSON.stringify(status);
    if (signature !== lastBatchStatus && (!localSubmitting || status.status !== 'idle')) showBatchResult(status);
    lastBatchStatus = signature;
    if (serverRunning) { clearBatch(); }
  } catch (error) { if (submitting()) $('batchResult').textContent = '无法读取执行进度；请保留页面并人工核对学校结果，不要重复提交。'; }
  finally { statusBusy = false; updateControls(); }
}

$('applyBatch').onclick = () => {
  if (submitting() || schoolBusy || !schoolReady) return;
  for (const row of $('schoolRows').querySelectorAll('tr')) {
    row.querySelector('.batch-method').value = $('batchMethod').value;
    row.querySelector('.batch-note').value = $('batchNote').value.trim();
  }
  saveDrafts();
  clearBatch();
};
for (const event of ['input', 'change']) $('schoolRows').addEventListener(event, () => { saveDrafts(); clearBatch(); });
for (const id of ['batchMethod', 'batchNote']) $(id).oninput = clearBatch;
$('prepareBatch').onclick = async () => {
  if (submitting() || schoolBusy || previewPreparing || !schoolVerified) return;
  clearBatch();
  const generation = batchGeneration, expectedRevision = schoolRevision, records = collectDrafts();
  previewPreparing = true;
  updateControls();
  try {
    const preview = await api('/api/prepare-batch', {revision: expectedRevision, records});
    if (generation !== batchGeneration || expectedRevision !== schoolRevision || submitting() || !schoolVerified) return;
    batchToken = preview.token;
    $('batchPreview').textContent = preview.date + ' · 共 ' + preview.count + ' 人\n' +
      preview.records.map(record => record.name + ' · ' + record.id + ' · ' + record.method + ' · ' + record.explanation).join('\n') + '\n' + preview.notice;
    batchExpiry = setTimeout(() => { clearBatch(); $('batchPreview').textContent = '预览已过期，请重新生成。'; }, 300000);
  } catch (error) { if (generation === batchGeneration) $('batchResult').textContent = error.message; }
  finally { previewPreparing = false; updateControls(); }
};
$('batchAck').onchange = updateControls;
$('submitBatch').onclick = async () => {
  if (submitting() || schoolBusy || !batchToken || !$('batchAck').checked) return;
  const token = batchToken;
  localSubmitting = true;
  clearBatch();
  $('batchResult').textContent = '正在官方页面依次提交并逐条核对；请保留面板和独立 Edge。';
  try { showBatchResult(await api('/api/batch-submit', {token, supervised_approved: true})); }
  catch (error) { $('batchResult').textContent = error.message; }
  finally {
    localSubmitting = false;
    await readBatchStatus();
    updateControls();
    if (!serverRunning) { await readSchool(); await load(); readAllRoster(); }
  }
};

$('oneClickSubmit').onclick = async () => {
  if (submitting() || schoolBusy || previewPreparing || !schoolReady || !schoolRecords.length || !$('schoolAck').checked) return;
  localSubmitting = true;
  updateControls();
  clearBatch();
  $('batchResult').textContent = '一键代确认已开始：正在保存今日名单核验快照…';
  try {
    if (!schoolVerified) {
      const verified = await api('/api/verify-school', {acknowledged: true, revision: schoolRevision});
      schoolVerified = true;
      $('state').textContent = '今日学校名单已核验，名单一致';
      $('schoolVerification').textContent = '今日学校名单已核验 · 核验时间 ' + verified.at;
    }
    for (const row of $('schoolRows').querySelectorAll('tr')) {
      row.querySelector('.batch-method').value = $('batchMethod').value;
      row.querySelector('.batch-note').value = $('batchNote').value.trim();
    }
    saveDrafts();
    const preview = await api('/api/prepare-batch', {revision: schoolRevision, records: collectDrafts()});
    $('batchPreview').textContent = preview.date + ' · 共 ' + preview.count + ' 人\n' +
      preview.records.map(record => record.name + ' · ' + record.id + ' · ' + record.method + ' · ' + record.explanation).join('\n') + '\n' + preview.notice;
    $('batchResult').textContent = '预览已生成，正在立即提交；请保留面板和独立 Edge，不要重复点击。';
    showBatchResult(await api('/api/batch-submit', {token: preview.token, supervised_approved: true}));
  } catch (error) {
    $('batchResult').textContent = error.message + ' 一键代确认已中止；如官方页面可能已有提交，请人工核对其记录，今天不会自动重试。';
  } finally {
    localSubmitting = false;
    await readBatchStatus();
    updateControls();
    if (!serverRunning) { await readSchool(); await load(); readAllRoster(); }
  }
};

$('refresh').onclick = readSchool;
$('readSchool').onclick = readSchool;
$('openSchool').onclick = () => schoolAction('/api/open-school', {});
$('openRoster').onclick = () => schoolAction('/api/open-roster', {date: $('rosterDate').value});
$('schoolAck').onchange = updateControls;
$('verifySchool').onclick = async () => {
  if (submitting() || schoolBusy || !$('schoolAck').checked) return;
  schoolBusy = true;
  updateControls();
  try { await api('/api/verify-school', {acknowledged: true, revision: schoolRevision}); }
  catch (error) { schoolError(error); }
  finally { schoolBusy = false; updateControls(); await readSchool(); await load(); }
};
$('saveTime').onclick = async () => {
  try { await api('/api/settings', {time: $('runAt').value}); await load(); }
  catch (error) { alert(error.message); }
};
$('ack').onchange = event => $('verify').disabled = !event.target.checked;
$('verify').onclick = async () => {
  try { await api('/api/verify', {acknowledged: true, revision}); await load(); }
  catch (error) { alert(error.message); }
};
function showGateStatus(page) {
  const waiting = page.status !== 'connected';
  $('loginGate').classList.toggle('gate-waiting', waiting);
  $('gateStatus').textContent = waiting ? (gateWaiting[page.status] || page.status || '状态未知') : '登录成功，正在进入工具…';
  $('gateHint').hidden = page.status !== 'login_required';
  $('gateLogin').textContent = page.status === 'endpoint_unavailable' ? '打开校园 H5 登录' : '重新打开校园 H5 登录';
}
async function gateCheck() {
  if (!gateOpen || gateBusy) return;
  gateBusy = true;
  try {
    const page = await api('/api/school-page');
    if (!gateOpen) return;
    if (page.status === 'connected') { enterTool(page); return; }
    showGateStatus(page);
  } catch (error) {
    if (!gateOpen) return;
    if (serverRunning) { enterTool(null); return; }
    $('loginGate').classList.add('gate-waiting');
    $('gateStatus').textContent = '暂时无法读取学校状态，稍后将自动重试。';
  } finally {
    gateBusy = false;
    if (gateOpen) gateTimer = setTimeout(gateCheck, 3000);
  }
}
async function autoOpenRoster(page) {
  // After the login gate passes, read today's pending roster without the
  // extra click.  Skip when a batch is running, when another school action is
  // in flight, or when today's roster is already loaded on the board.
  if (!page || submitting() || schoolBusy) return;
  const date = $('rosterDate').value;
  if (!date || (page.roster_ready && page.business_date === date)) return;
  schoolBusy = true;
  updateControls();
  $('schoolStatus').textContent = '登录成功，正在自动读取今日待确认名单…';
  try { showSchool(await api('/api/open-roster', {date})); }
  catch (error) { schoolError(error); }
  finally { schoolBusy = false; updateControls(); }
}
function enterTool(page) {
  if (!gateOpen) return;
  gateOpen = false;
  clearTimeout(gateTimer);
  const gate = $('loginGate');
  gate.classList.remove('gate-waiting');
  gate.classList.add('done');
  setTimeout(() => { gate.hidden = true; }, 500);
  if (page) showSchool(page);
  else if (!serverRunning) readSchool();
  updateControls();
  autoOpenRoster(page);
}
$('gateLogin').onclick = async () => {
  if (gateBusy) return;
  gateBusy = true;
  $('gateStatus').textContent = '正在打开学校登录窗口…';
  try {
    await api('/api/open-school', {});
    $('gateHint').hidden = false;
    $('gateStatus').textContent = '学校页面已打开，等待登录…';
  } catch (error) { $('gateStatus').textContent = error.message; }
  finally { gateBusy = false; if (gateOpen) gateCheck(); }
};
$('gateSkip').onclick = () => enterTool(null);
async function initialize() {
  await load();
  await readBatchStatus();
  if (serverRunning) { enterTool(null); return; }
  if (!submitting()) await readSchool();
  if (gateOpen) { gateCheck(); $('gateLogin').focus(); }
}
initialize();
setInterval(readBatchStatus, 2000);
setInterval(() => { if (!submitting()) { load(); readSchool(); } }, 60000);
