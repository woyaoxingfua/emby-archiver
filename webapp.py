from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from app_service import AppService


HTML_PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>Emby Cache Tool Web</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 0; background:#111827; color:#e5e7eb; }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 16px; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:16px; }
    .card { background:#1f2937; border-radius:10px; padding:16px; }
    input, button, textarea { font: inherit; }
    input { padding:8px; border-radius:6px; border:1px solid #4b5563; background:#111827; color:#e5e7eb; }
    button { padding:8px 12px; border:0; border-radius:6px; background:#2563eb; color:white; cursor:pointer; }
    button.alt { background:#374151; }
    button.warn { background:#dc2626; }
    table { width:100%; border-collapse: collapse; font-size:14px; }
    th, td { padding:8px; border-bottom:1px solid #374151; text-align:left; vertical-align: top; }
    .row { display:flex; gap:8px; flex-wrap:wrap; margin-bottom:10px; align-items:center; }
    .logs { height:320px; overflow:auto; white-space:pre-wrap; background:#0b1220; padding:12px; border-radius:8px; font-family: monospace; font-size:13px; }
    .log-line { display:block; padding:1px 0; }
    .log-line:hover { background:#1e293b; }
    .log-line .num { color:#6b7280; display:inline-block; min-width:40px; margin-right:8px; user-select:none; }
    .small { color:#9ca3af; font-size:13px; }
    .path { max-width: 280px; word-break: break-all; }
    .dl-title { font-size:16px; font-weight:600; margin-bottom:8px; }
    .dl-row { display:flex; gap:10px; padding:3px 0; font-size:14px; }
    .dl-label { color:#9ca3af; min-width:48px; flex-shrink:0; }
    .bar { height:10px; background:#0b1220; border-radius:6px; overflow:hidden; margin-top:10px; position:relative; }
    .bar-fill { height:100%; background:#2563eb; width:0%; transition: width .3s ease; }
    .bar-indeterminate .bar-fill { width:40%; position:absolute; animation: dl-indet 1.2s infinite ease-in-out; }
    @keyframes dl-indet { 0% { left:-40%; } 100% { left:100%; } }
    .level-tag { display:inline-block; padding:1px 6px; border-radius:3px; font-size:11px; margin-right:6px; }
    .level-INFO { background:#1e3a5f; color:#93c5fd; }
    .level-WARNING { background:#4a3a1a; color:#fcd34d; }
    .level-ERROR { background:#4a1a1a; color:#fca5a5; }
    .level-DEBUG { background:#2d1a4a; color:#c4b5fd; }
    .filter-btn { padding:4px 10px; font-size:12px; border-radius:4px; border:1px solid #4b5563; background:#111827; color:#9ca3af; cursor:pointer; }
    .filter-btn.active { background:#2563eb; color:white; border-color:#2563eb; }
    .segments-input { width:60px; }
  </style>
</head>
<body>
<div class="wrap">
  <h1>Emby Cache Tool Web</h1>
  <div class="small" id="summary"></div>
  <div class="grid">
    <div class="card">
      <h2>控制</h2>
      <div class="row">
        <button onclick="loginTest()">测试登录</button>
        <button class="alt" onclick="pauseCurrent()">暂停</button>
        <button class="alt" onclick="resumeCurrent()">继续</button>
        <button class="warn" onclick="cancelCurrent()">取消</button>
      </div>
      <div class="row">
        <label>并发数</label>
        <input type="number" class="segments-input" id="segmentsInput" min="1" max="32" value="8">
        <button onclick="setSegments()">设置并发</button>
      </div>
      <div class="row">
        <input id="defaultDir" style="flex:1" placeholder="默认下载目录">
        <button onclick="setDefaultDir()">设置默认目录</button>
        <label><input type="checkbox" id="persistDefaultDir"> 同时保存为永久默认（写入 config.json）</label>
      </div>
      <div class="row">
        <input id="keyword" style="flex:1" placeholder="搜索关键词">
        <button onclick="searchItems()">搜索</button>
      </div>
      <div class="row">
        <input id="overrideDir" style="flex:1" placeholder="本次下载目录（可留空）">
      </div>
      <div class="row">
        <label><input type="checkbox" id="forceRestart"> 忽略 .part 重新开始</label>
      </div>
      <div id="status" class="small"></div>
      <h3>搜索结果</h3>
      <table id="resultsTable"><thead><tr><th>ID</th><th>类型</th><th>标题</th><th>操作</th></tr></thead><tbody></tbody></table>
    </div>
    <div class="card">
      <h2>当前下载</h2>
      <div id="currentState" class="small"></div>
      <h3>下载记录</h3>
      <table id="downloadsTable"><thead><tr><th>状态</th><th>标题</th><th>进度</th><th>模式</th><th>路径</th><th>操作</th></tr></thead><tbody></tbody></table>
    </div>
  </div>
  <div class="card" style="margin-top:16px;">
    <h2>日志</h2>
    <div class="small">Web 模式不直接调用浏览器所在设备的本地播放器；完成项仅展示保存路径。</div>
    <div class="row" style="margin-top:10px;">
      <select id="logFile" onchange="switchLogFile()">
        <option value="__realtime__">实时进程日志</option>
      </select>
      <button onclick="refreshLogFile()">刷新</button>
      <button class="alt" onclick="loadMoreLog()">加载更多</button>
      <label><input type="checkbox" id="autoScroll" checked> 自动滚动到底部</label>
    </div>
    <div class="row">
      <span class="small">级别：</span>
      <button class="filter-btn active" data-level="" onclick="setLogLevel(this)">全部</button>
      <button class="filter-btn" data-level="INFO" onclick="setLogLevel(this)">INFO</button>
      <button class="filter-btn" data-level="WARNING" onclick="setLogLevel(this)">WARNING</button>
      <button class="filter-btn" data-level="ERROR" onclick="setLogLevel(this)">ERROR</button>
      <button class="filter-btn" data-level="DEBUG" onclick="setLogLevel(this)">DEBUG</button>
      <input id="logSearch" placeholder="过滤关键词" oninput="filterLog()" style="flex:1; min-width:120px;">
    </div>
    <div id="logs" class="logs"></div>
  </div>
</div>
<script>
// 日志面板状态
const logState = {
  currentFile: '__realtime__',  // '__realtime__' 表示进程内实时日志
  level: '',
  offset: 0,
  limit: 200,
  allLines: [],       // 当前已加载的所有行（用于前端搜索过滤）
  total: 0,
  filtered: 0,
};

async function api(path, options={}) {
  const res = await fetch(path, {
    headers: {'Content-Type': 'application/json'},
    ...options,
  });
  const text = await res.text();
  let data;
  try { data = text ? JSON.parse(text) : {}; } catch { data = {detail: text}; }
  if (!res.ok) throw new Error(data.detail || text || 'Request failed');
  return data;
}

function escapeHtml(text) {
  return String(text ?? '').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s]));
}

function detectLevel(line) {
  const m = line.match(/\\[(INFO|WARNING|ERROR|DEBUG)\\]/);
  return m ? m[1] : null;
}

function renderLogLines(lines) {
  const search = document.getElementById('logSearch').value.trim().toLowerCase();
  const container = document.getElementById('logs');
  container.innerHTML = '';
  let idx = 0;
  for (const line of lines) {
    if (search && !line.toLowerCase().includes(search)) continue;
    const div = document.createElement('div');
    div.className = 'log-line';
    const level = detectLevel(line);
    const num = document.createElement('span');
    num.className = 'num';
    num.textContent = String(idx + 1);
    div.appendChild(num);
    if (level) {
      const tag = document.createElement('span');
      tag.className = 'level-tag level-' + level;
      tag.textContent = level;
      div.appendChild(tag);
    }
    const text = document.createElement('span');
    text.innerHTML = escapeHtml(line);
    div.appendChild(text);
    container.appendChild(div);
    idx++;
  }
}

async function refreshLogFile() {
  const file = logState.currentFile;
  if (file === '__realtime__') {
    // 实时进程日志
    try {
      const data = await api('/api/logs?limit=500');
      logState.allLines = data.logs || [];
      logState.total = logState.allLines.length;
      logState.filtered = logState.allLines.length;
      logState.offset = 0;
      renderLogLines(logState.allLines);
      if (document.getElementById('autoScroll').checked) {
        const container = document.getElementById('logs');
        container.scrollTop = container.scrollHeight;
      }
    } catch (e) {
      document.getElementById('logs').textContent = String(e.message || e);
    }
  } else {
    // 文件日志
    logState.offset = 0;
    await loadFileLog();
  }
}

async function loadFileLog() {
  const file = logState.currentFile;
  if (file === '__realtime__') return;
  const params = new URLSearchParams({
    name: file,
    offset: String(logState.offset),
    limit: String(logState.limit),
  });
  if (logState.level) params.set('level', logState.level);
  try {
    const data = await api('/api/log-file?' + params.toString());
    logState.allLines = data.lines || [];
    logState.total = data.total;
    logState.filtered = data.filtered;
    renderLogLines(logState.allLines);
    if (document.getElementById('autoScroll').checked && logState.offset === 0) {
      const container = document.getElementById('logs');
      container.scrollTop = container.scrollHeight;
    }
  } catch (e) {
    document.getElementById('logs').textContent = String(e.message || e);
  }
}

async function loadMoreLog() {
  const file = logState.currentFile;
  if (file === '__realtime__') return;
  logState.offset += logState.limit;
  const params = new URLSearchParams({
    name: file,
    offset: String(logState.offset),
    limit: String(logState.limit),
  });
  if (logState.level) params.set('level', logState.level);
  try {
    const data = await api('/api/log-file?' + params.toString());
    logState.allLines = data.lines || [];
    logState.total = data.total;
    logState.filtered = data.filtered;
    renderLogLines(logState.allLines);
  } catch (e) {
    document.getElementById('logs').textContent = String(e.message || e);
  }
}

function setLogLevel(btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  logState.level = btn.dataset.level;
  logState.offset = 0;
  refreshLogFile();
}

function filterLog() {
  renderLogLines(logState.allLines);
}

async function switchLogFile() {
  const sel = document.getElementById('logFile');
  logState.currentFile = sel.value;
  logState.offset = 0;
  logState.allLines = [];
  await refreshLogFile();
}

async function populateLogFiles() {
  const sel = document.getElementById('logFile');
  // 保留第一个 option（实时进程日志）
  sel.innerHTML = '<option value="__realtime__">实时进程日志</option>';
  try {
    const files = await api('/api/log-files');
    for (const f of files) {
      const opt = document.createElement('option');
      opt.value = f.name;
      const size = f.size >= 1024 ? (f.size / 1024).toFixed(1) + ' KB' : f.size + ' B';
      opt.textContent = f.name + '  (' + size + ', ' + f.mtime + ')';
      sel.appendChild(opt);
    }
  } catch (e) {
    // ignore
  }
}

function renderCurrentState(state) {
  const el = document.getElementById('currentState');
  const itemName = state && state.item_name ? escapeHtml(state.item_name) : '';
  if (!itemName) {
    el.innerHTML = '<div class="small">无进行中的下载</div>';
    return;
  }
  const mode = escapeHtml(state.mode || '未知模式');
  const bytesText = (state.bytes_text && String(state.bytes_text).trim())
    ? escapeHtml(state.bytes_text)
    : `${escapeHtml(String(state.downloaded || 0))} / ${escapeHtml(state.expected != null ? String(state.expected) : '--')}`;
  const pct = (typeof state.percent === 'number' && isFinite(state.percent)) ? state.percent : null;
  const pctText = pct != null ? `${pct.toFixed(1)}%` : '--';
  const speedText = escapeHtml(state.speed_text || '--');
  const etaText = escapeHtml(state.eta_text || '--');
  const detail = state.detail ? `<div class="dl-row"><span class="dl-label">详情</span><span>${escapeHtml(state.detail)}</span></div>` : '';
  let barHtml;
  if (pct != null) {
    const width = Math.max(0, Math.min(100, pct)).toFixed(1);
    barHtml = `<div class="bar"><div class="bar-fill" style="width:${width}%"></div></div>`;
  } else {
    barHtml = `<div class="bar bar-indeterminate"><div class="bar-fill"></div></div>`;
  }
  el.innerHTML = `
    <div class="dl-title">${itemName}</div>
    <div class="dl-row"><span class="dl-label">模式</span><span>${mode}</span></div>
    <div class="dl-row"><span class="dl-label">进度</span><span>${bytesText}</span></div>
    <div class="dl-row"><span class="dl-label">百分比</span><span>${pctText}</span></div>
    <div class="dl-row"><span class="dl-label">速度</span><span>${speedText}</span></div>
    <div class="dl-row"><span class="dl-label">剩余</span><span>${etaText}</span></div>
    ${detail}
    ${barHtml}
  `;
}

async function refreshAll() {
  try {
    const summary = await api('/api/summary');
    let summaryText = `服务器: ${summary.server_url} | 默认目录: ${summary.download_dir} | segments=${summary.segments} | stream_segments=${summary.stream_segments}`;
    if (summary.proxy_http || summary.proxy_https) {
      summaryText += ` | 代理: HTTP=${summary.proxy_http || '无'} HTTPS=${summary.proxy_https || '无'}`;
    }
    if (summary.experimental_force_multipart) {
      summaryText += ' | 强制多段: 开';
    }
    document.getElementById('summary').textContent = summaryText;
    document.getElementById('defaultDir').value = summary.download_dir || '';
    document.getElementById('segmentsInput').value = summary.segments || 8;
  } catch (e) {
    document.getElementById('summary').textContent = String(e.message || e);
  }
  try {
    const state = await api('/api/status');
    renderCurrentState(state);
  } catch (e) {
    document.getElementById('currentState').textContent = String(e.message || e);
  }
  try {
    const data = await api('/api/downloads');
    const tbody = document.querySelector('#downloadsTable tbody');
    tbody.innerHTML = data.records.map(r => {
      const progress = r.expected_size ? `${r.bytes_downloaded}/${r.expected_size}` : `${r.bytes_downloaded}`;
      let actions = '';
      if (!['completed', 'cancelled'].includes(r.status)) {
        actions += `<button onclick="resumeRecord('${r.item_id}')">继续</button> `;
      }
      actions += `<button class="warn" onclick="deleteRecord('${r.item_id}', '${r.status}')">删除</button>`;
      return `<tr><td>${escapeHtml(r.status)}</td><td>${escapeHtml(r.item_name)}</td><td>${escapeHtml(progress)}</td><td>${escapeHtml(r.download_mode || '-')}</td><td class="path">${escapeHtml(r.target_path || '-')}</td><td>${actions}</td></tr>`;
    }).join('');
  } catch (e) {
    document.querySelector('#downloadsTable tbody').innerHTML = `<tr><td colspan="6">${escapeHtml(e.message || e)}</td></tr>`;
  }
  // 刷新日志（仅实时模式自动刷新，文件模式按需）
  if (logState.currentFile === '__realtime__') {
    try {
      const data = await api('/api/logs?limit=500');
      logState.allLines = data.logs || [];
      logState.total = logState.allLines.length;
      logState.filtered = logState.allLines.length;
      renderLogLines(logState.allLines);
      if (document.getElementById('autoScroll').checked) {
        const container = document.getElementById('logs');
        container.scrollTop = container.scrollHeight;
      }
    } catch (e) {
      document.getElementById('logs').textContent = String(e.message || e);
    }
  }
}

async function loginTest() {
  try {
    const data = await api('/api/login-test', {method:'POST'});
    document.getElementById('status').textContent = `登录成功: ${data.user.Name} (${data.user.Id})`;
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function searchItems() {
  const keyword = document.getElementById('keyword').value.trim();
  if (!keyword) return;
  try {
    const data = await api(`/api/search?keyword=${encodeURIComponent(keyword)}&limit=50`);
    const tbody = document.querySelector('#resultsTable tbody');
    tbody.innerHTML = data.results.map(r => `
      <tr>
        <td>${escapeHtml(r.item_id)}</td>
        <td>${escapeHtml(r.type)}</td>
        <td>${escapeHtml(r.name)}</td>
        <td><button onclick="startDownload('${r.item_id}')">下载</button></td>
      </tr>`).join('');
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
}

async function startDownload(itemId) {
  const overrideDir = document.getElementById('overrideDir').value.trim();
  const forceRestart = document.getElementById('forceRestart').checked;
  try {
    await api('/api/download', {
      method:'POST',
      body: JSON.stringify({item_id:itemId, force_restart:forceRestart, override_dir: overrideDir || null})
    });
    document.getElementById('status').textContent = '下载任务已启动';
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function resumeRecord(itemId) {
  try {
    await api(`/api/downloads/${itemId}/resume`, {method:'POST'});
    document.getElementById('status').textContent = '已请求继续下载';
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function deleteRecord(itemId, status) {
  let msg = '确认删除本地文件和下载记录？';
  if (status !== 'completed' && status !== 'cancelled') {
    msg = '该记录尚未完成！\\n\\n确认删除将：\\n- 取消当前下载\\n- 删除已下载的临时文件\\n- 删除下载记录\\n\\n确认继续？';
  }
  if (!confirm(msg)) return;
  try {
    await api(`/api/downloads/${itemId}`, {method:'DELETE'});
    document.getElementById('status').textContent = '已删除记录';
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function setDefaultDir() {
  const path = document.getElementById('defaultDir').value.trim();
  const persist = document.getElementById('persistDefaultDir').checked;
  try {
    await api('/api/default-download-dir', {method:'POST', body: JSON.stringify({path, persist})});
    document.getElementById('status').textContent = persist ? '默认下载目录已更新并写入配置' : '默认下载目录已更新（仅本次会话）';
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function pauseCurrent() {
  try {
    await api('/api/control/pause', {method:'POST'});
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function resumeCurrent() {
  try {
    await api('/api/control/resume', {method:'POST'});
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function cancelCurrent() {
  const cleanup = confirm('是否同时删除 .part 临时文件？\\n\\n点确定=删除临时文件，点取消=保留临时文件');
  try {
    await api('/api/control/cancel', {method:'POST', body: JSON.stringify({cleanup_temp: cleanup})});
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

async function setSegments() {
  const input = document.getElementById('segmentsInput');
  let n = parseInt(input.value, 10);
  if (isNaN(n) || n < 1) n = 1;
  if (n > 32) n = 32;
  try {
    const data = await api('/api/control/segments', {method:'POST', body: JSON.stringify({segments: n})});
    document.getElementById('status').textContent = `并发数已设置为 ${data.segments}`;
  } catch (e) {
    document.getElementById('status').textContent = e.message || e;
  }
  refreshAll();
}

setInterval(refreshAll, 3000);
refreshAll();
populateLogFiles();
</script>
</body>
</html>
"""


async def _index(_request: Request) -> Response:
    return HTMLResponse(HTML_PAGE)


async def _summary(request: Request) -> Response:
    service = request.app.state.service
    return _api_call(lambda: service.config_summary())


async def _status(request: Request) -> Response:
    service = request.app.state.service
    return JSONResponse(service.current_status())


async def _logs(request: Request) -> Response:
    service = request.app.state.service
    query = parse_qs(request.url.query)
    raw_limit = query.get("limit", ["200"])[0]
    try:
        limit = max(1, min(500, int(raw_limit)))
    except ValueError:
        limit = 200
    return JSONResponse({"logs": service.logs(limit)})


async def _log_files(request: Request) -> Response:
    service = request.app.state.service
    return _api_call(lambda: service.list_log_files())


async def _log_file(request: Request) -> Response:
    service = request.app.state.service
    query = parse_qs(request.url.query)
    name = query.get("name", [""])[0].strip()
    if not name:
        return _error_response("name is required")
    raw_offset = query.get("offset", ["0"])[0]
    raw_limit = query.get("limit", ["200"])[0]
    level = query.get("level", [""])[0].strip() or None
    try:
        offset = max(0, int(raw_offset))
    except ValueError:
        offset = 0
    try:
        limit = max(1, min(2000, int(raw_limit)))
    except ValueError:
        limit = 200
    return _api_call(lambda: service.read_log_file(name, offset=offset, limit=limit, level=level))


async def _login_test(request: Request) -> Response:
    service = request.app.state.service
    return _api_call(lambda: {"user": service.login_test()})


async def _search(request: Request) -> Response:
    service = request.app.state.service
    query = parse_qs(request.url.query)
    keyword = query.get("keyword", [""])[0].strip()
    if not keyword:
        return _error_response("keyword is required")
    raw_limit = query.get("limit", ["50"])[0]
    try:
        limit = max(1, min(200, int(raw_limit)))
    except ValueError:
        limit = 50
    return _api_call(lambda: {"results": service.search(keyword, limit=limit)})


async def _downloads(request: Request) -> Response:
    service = request.app.state.service
    return _api_call(lambda: {"records": service.list_downloads()})


async def _start_download(request: Request) -> Response:
    service = request.app.state.service
    payload = await _json_body(request)
    item_id = str(payload.get("item_id", "")).strip()
    if not item_id:
        return _error_response("item_id is required")
    override_dir = payload.get("override_dir")
    force_restart = bool(payload.get("force_restart", False))
    return _api_call(
        lambda: _ok(
            service.start_download(
                item_id,
                force_restart=force_restart,
                override_dir=Path(str(override_dir)) if override_dir else None,
            )
        )
    )


async def _resume_download(request: Request) -> Response:
    service = request.app.state.service
    item_id = request.path_params["item_id"]
    return _api_call(lambda: _ok(service.resume_record(item_id)))


async def _delete_download(request: Request) -> Response:
    service = request.app.state.service
    item_id = request.path_params["item_id"]
    return _api_call(lambda: service.delete_record(item_id))


async def _pause(request: Request) -> Response:
    service = request.app.state.service
    return _api_call(lambda: _ok(service.pause_current()))


async def _resume(request: Request) -> Response:
    service = request.app.state.service
    return _api_call(lambda: _ok(service.resume_current()))


async def _cancel(request: Request) -> Response:
    service = request.app.state.service
    payload = await _json_body(request)
    cleanup_temp = bool(payload.get("cleanup_temp", False))
    return _api_call(lambda: _ok(service.cancel_current(cleanup_temp=cleanup_temp)))


async def _set_segments(request: Request) -> Response:
    service = request.app.state.service
    payload = await _json_body(request)
    raw = payload.get("segments")
    if raw is None:
        return _error_response("segments is required")
    try:
        n = int(raw)
    except (ValueError, TypeError):
        return _error_response("segments must be an integer")
    return _api_call(lambda: {"segments": service.set_segments(n)})


async def _default_download_dir(request: Request) -> Response:
    service = request.app.state.service
    payload = await _json_body(request)
    path = str(payload.get("path", "")).strip()
    if not path:
        return _error_response("path is required")
    persist = bool(payload.get("persist", False))
    return _api_call(lambda: {"path": service.set_default_download_dir(Path(path), persist=persist)})


async def _json_body(request: Request) -> dict[str, Any]:
    if not request.headers.get("content-type", "").lower().startswith("application/json"):
        return {}
    data = await request.json()
    return data if isinstance(data, dict) else {}


def _ok(_value: Any = None) -> dict[str, Any]:
    return {"ok": True}


def _api_call(func) -> Response:
    try:
        return JSONResponse(func())
    except Exception as exc:
        return _error_response(str(exc))


def _error_response(message: str, status_code: int = 400) -> Response:
    return JSONResponse({"detail": message}, status_code=status_code)


def create_web_app(service: AppService) -> Starlette:
    app = Starlette(
        routes=[
            Route("/", _index, methods=["GET"]),
            Route("/api/summary", _summary, methods=["GET"]),
            Route("/api/status", _status, methods=["GET"]),
            Route("/api/logs", _logs, methods=["GET"]),
            Route("/api/log-files", _log_files, methods=["GET"]),
            Route("/api/log-file", _log_file, methods=["GET"]),
            Route("/api/login-test", _login_test, methods=["POST"]),
            Route("/api/search", _search, methods=["GET"]),
            Route("/api/downloads", _downloads, methods=["GET"]),
            Route("/api/download", _start_download, methods=["POST"]),
            Route("/api/downloads/{item_id:str}/resume", _resume_download, methods=["POST"]),
            Route("/api/downloads/{item_id:str}", _delete_download, methods=["DELETE"]),
            Route("/api/control/pause", _pause, methods=["POST"]),
            Route("/api/control/resume", _resume, methods=["POST"]),
            Route("/api/control/cancel", _cancel, methods=["POST"]),
            Route("/api/control/segments", _set_segments, methods=["POST"]),
            Route("/api/default-download-dir", _default_download_dir, methods=["POST"]),
        ]
    )
    app.state.service = service
    return app
