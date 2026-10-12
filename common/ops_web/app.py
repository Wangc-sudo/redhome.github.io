"""ops-web 应用装配：权限管理（grant 增删 + 审计流水 + 批量区域开通）
与定时任务管理（管道列表 / 开关 / 新增 / 立即运行一次 / 执行流水）。

访问控制双闸（铁律 8）：

* 网络层：compose 仅回环绑定（127.0.0.1:18100），不对局域网暴露；
* 应用层：除 ``/auth/*`` 外的每个路由都要求 session 身份且
  ``viewer.is_admin``——ops-web 不是面向全员的服务。

输出纪律与 bi-web 一致：错误体只用泛化词汇（unauthorized / forbidden /
unavailable / bad_request），异常只记一条带类名的 WARNING；页面 HTML 的
每一处插值都过 ``html.escape``（管理页同样无反射面）。

写路径（铁律 3 的另一半）：``bi_authz_grant`` 的 INSERT/DELETE 与定时
任务管理的 Nacos 发布 / run-request 写入只存在于本模块，且每次变更与
同事务的一行 audit 同生共死（run_request 行的 requested_by 即触发审计）。
"""

import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from contextlib import contextmanager
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from common.bi_web import auth, authz
from common.public_data import (
    bi_authz,
    calendar_store,
    channel_target,
    ops_control,
    recipient_override,
    report_roster,
)
from common.public_data.calendar_import import CalendarImportError
from common.public_data.channel_target import CHANNELS
from common.public_data.pipeline_config import PipelineConfig
from common.public_data.pipeline_taxonomy import (
    CATEGORY_FAMILY,
    SUPPORTED_CATEGORIES,
    resolve_category,
)
from common.public_data.scheduler import has_command

_LOGGER = logging.getLogger(__name__)


class ErrorDetail:
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    UNAVAILABLE = "unavailable"
    BAD_REQUEST = "bad_request"


def load_settings():
    from common.public_data.settings import Settings
    return Settings.from_environment()


def connect(database_settings):
    from common.public_data.db import connect as _connect
    return _connect(database_settings)


def _mart_connector(settings):
    @contextmanager
    def connector():
        connection = connect(settings.mart_database)
        try:
            yield connection
        finally:
            connection.close()

    return connector


# ---------------------------------------------------------------------------
# 极简服务端渲染（零模板依赖；所有插值过 html.escape）
# ---------------------------------------------------------------------------

_PAGE_STYLE = (
    "body{margin:0;background:#f5f6f8;color:#333;font-family:-apple-system,"
    "'PingFang SC','Microsoft YaHei',sans-serif}"
    "header{background:#1f3a5f;color:#fff;padding:12px 24px;display:flex;"
    "align-items:center;gap:24px}"
    "header a{color:#cfe0f5;text-decoration:none;font-size:14px}"
    "header nav{display:flex;gap:20px;align-items:center;flex-wrap:wrap}"
    "header .nav-group{display:flex;align-items:center;gap:10px}"
    "header .nav-label{font-size:12px;color:#7f9bc4;padding-right:8px;"
    "border-right:1px solid #35547d}"
    "header .who{margin-left:auto;font-size:13px;color:#9fb8d8}"
    "main{padding:24px;max-width:1080px;margin:0 auto}"
    "h1{font-size:18px}h2{font-size:15px;margin-top:32px}"
    "table{border-collapse:collapse;width:100%;background:#fff;font-size:13px}"
    "th,td{border:1px solid #e3e6ea;padding:6px 10px;text-align:left}"
    "th{background:#f0f2f5}"
    "form.inline{display:flex;gap:8px;flex-wrap:wrap;background:#fff;"
    "padding:12px;border:1px solid #e3e6ea;margin-top:12px}"
    "input,select{padding:5px 8px;font-size:13px}"
    "button{padding:6px 14px;font-size:13px;cursor:pointer}"
    ".hint{color:#888;font-size:12px}"
    ".modal-mask{display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);"
    "align-items:center;justify-content:center;z-index:99}"
    ".modal-box{background:#fff;padding:20px 24px;border-radius:8px;"
    "min-width:280px;max-width:420px}"
    ".modal-btns{margin-top:16px;text-align:right}"
    ".modal-btns button{margin-left:8px}"
    # 工作日历页（2026-10-10）：月历网格 + 三源徽标
    ".cal-nav{display:flex;gap:12px;align-items:center;margin:14px 0 6px}"
    ".cal-nav a{color:#1f3a5f;text-decoration:none;font-size:13px}"
    ".cal-table{table-layout:fixed;background:#fff}"
    ".cal-table th{text-align:center;padding:4px}"
    ".cal-table td{height:56px;vertical-align:top;padding:4px 6px;font-size:13px}"
    ".cal-day{cursor:pointer}"
    ".cal-day:hover{outline:1px solid #1f3a5f;outline-offset:-1px}"
    ".cal-rest{background:#e8f0fd;color:#1f3a5f}"
    ".cal-work{background:#fff}"
    ".cal-today{outline:2px solid #1f3a5f;outline-offset:-2px}"
    ".cal-empty{background:#fafafa;color:#c8cdd4}"
    ".cal-src{font-size:10px;padding:0 4px;border-radius:3px;margin-left:4px}"
    ".cal-src-holiday_cn{background:#d8e8ff;color:#1f3a5f}"
    ".cal-src-manual{background:#ffe9c7;color:#8a5a00}"
    ".cal-src-rule_fallback{background:#ececf0;color:#777}"
    ".flash{margin:12px 0;padding:10px 14px;border-radius:6px;font-size:14px}"
    ".flash.ok{background:#e6f4ea;color:#1e6b3a;border:1px solid #b7e0c4}"
    ".flash.err{background:#fdecea;color:#a12622;border:1px solid #f3c1bd}"
    # 定时任务页双视角 tab（2026-10-12）：按管理群（默认）/ 按功能类别
    ".tabs{display:flex;gap:8px;margin:16px 0 0}"
    ".tabs a{padding:6px 14px;border:1px solid #d0d5dd;border-bottom:none;"
    "border-radius:6px 6px 0 0;background:#f0f2f5;color:#1f3a5f;"
    "text-decoration:none;font-size:13px}"
    ".tabs a.active{background:#fff;font-weight:600}"
)

_PAGE_JS = """
const _ERR_MSG = {400:'请求不合法，请检查填写内容', 401:'登录已过期，请重新登录',
  403:'没有操作权限', 404:'接口不存在', 409:'与现有数据冲突',
  503:'服务暂时不可用，请稍后重试'};
function showFlash(kind, msg) {
  const el = document.getElementById('flash');
  if (!el) return;
  el.className = 'flash ' + kind;
  el.textContent = msg;
  el.style.display = 'block';
}
async function postJSON(url, body, okMsg) {
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  } catch (err) {
    showFlash('err', '操作失败：网络异常，请重试');
    return;
  }
  if (resp.ok) {
    sessionStorage.setItem('ops_flash', okMsg || '操作成功，已保存');
    location.reload();
    return;
  }
  showFlash('err', '操作失败（' + resp.status + '）：'
    + (_ERR_MSG[resp.status] || '请稍后重试'));
}
document.addEventListener('DOMContentLoaded', function () {
  const msg = sessionStorage.getItem('ops_flash');
  if (msg) {
    sessionStorage.removeItem('ops_flash');
    showFlash('ok', msg);
  }
});
function submitGrant(ev) {
  ev.preventDefault();
  const f = ev.target;
  postJSON('/api/grants', {
    user_id: f.user_id.value.trim(),
    grant_type: f.grant_type.value,
    grant_key: f.grant_key.value.trim(),
    note: f.note.value.trim(),
  }, '授权已提交');
}
function revokeGrant(userId, grantType, grantKey) {
  if (!confirm('确认收回该授权？')) return;
  postJSON('/api/grants/delete', {
    user_id: userId, grant_type: grantType, grant_key: grantKey,
  }, '授权已收回');
}
function grantRegion(ev) {
  ev.preventDefault();
  const region = ev.target.region.value;
  if (!confirm('确认为 ' + region + ' 区域全部在职成员开通该区域授权？')) return;
  postJSON('/api/grants/region', {region: region}, '该区域已批量授权');
}
let _pendingAction = null;
function showConfirm(msg) {
  document.getElementById('confirm-text').textContent = msg;
  document.getElementById('confirm-mask').style.display = 'flex';
}
function closeConfirm() {
  document.getElementById('confirm-mask').style.display = 'none';
  _pendingAction = null;
}
function confirmOk() {
  const action = _pendingAction;
  closeConfirm();
  if (action) postJSON(action.url, action.body, action.okMsg);
}
function pipelineAction(sel) {
  const value = sel.value;
  sel.value = '';
  if (!value) return;
  const sid = sel.dataset.service;
  if (value === 'toggle') {
    const enable = sel.dataset.enabled !== '1';
    _pendingAction = {
      url: '/api/pipelines/toggle',
      body: {service_id: sid, enabled: enable},
      okMsg: (enable ? '已启用 ' : '已停用 ') + sid + '（≤30s 生效）',
    };
    showConfirm('确认' + (enable ? '启用' : '停用') + ' ' + sid +
                '？（下一个调度 tick 生效，≤30s）');
  } else if (value === 'runonce') {
    _pendingAction = {
      url: '/api/pipelines/run-once',
      body: {service_id: sid},
      okMsg: '已提交立即运行 ' + sid,
    };
    showConfirm('确认立即运行一次 ' + sid + '？（调度器下个 tick 认领触发；' +
                '提醒/催办/汇总类当日已发的会绕开去重重发）');
  } else if (value === 'schedule') {
    openScheduleEdit(sid, sel.dataset.schedule || '');
  }
}
/* ---- 改定时时间（2026-10-12：时 0-23 + 分 00/30 两维下拉，不手写 cron）- */
let _scheduleEditRaw = '';
/* 只换 cron 的分/时两段，保留日/月/周（与后端 /api/pipelines/update 同规则）。 */
function rewriteCronTime(cron, hour, minute) {
  const parts = (cron || '').trim().split(/\\s+/);
  if (parts.length === 5) {
    return minute + ' ' + hour + ' ' + parts[2] + ' ' + parts[3] + ' ' + parts[4];
  }
  return minute + ' ' + hour + ' * * *';
}
function openScheduleEdit(sid, cron) {
  const parts = (cron || '').trim().split(/\\s+/);
  let hour = 18, minute = 0;
  if (parts.length === 5 && /^\\d+$/.test(parts[0]) && /^\\d+$/.test(parts[1])) {
    minute = parseInt(parts[0], 10);
    hour = parseInt(parts[1], 10);
  }
  if (!(hour >= 0 && hour <= 23)) hour = 18;
  if (minute !== 30) minute = 0;
  _scheduleEditRaw = cron || '';
  document.getElementById('schedule-edit-service').textContent = sid;
  /* 规则一并显示（中文为主），与列表「定时规则」列同口径，消除信息割裂。 */
  document.getElementById('schedule-edit-rule').textContent =
    cronZh(cron) || (cron || '常驻');
  document.getElementById('schedule-edit-cron').textContent = cron || '—';
  document.getElementById('schedule-edit-hour').value = String(hour);
  document.getElementById('schedule-edit-minute').value = String(minute);
  updateScheduleEditPreview();
  document.getElementById('schedule-mask').style.display = 'flex';
}
function updateScheduleEditPreview() {
  const hour = parseInt(document.getElementById('schedule-edit-hour').value, 10);
  const minute = parseInt(document.getElementById('schedule-edit-minute').value, 10);
  const next = rewriteCronTime(_scheduleEditRaw, hour, minute);
  const el = document.getElementById('schedule-edit-preview');
  if (!el) return;
  const zh = cronZh(next);
  el.textContent = (zh ? '将改为：' + zh : '将改为') + '（' + next + '）';
}
function closeScheduleEdit() {
  document.getElementById('schedule-mask').style.display = 'none';
}
function submitScheduleEdit() {
  const sid = document.getElementById('schedule-edit-service').textContent;
  const hour = parseInt(document.getElementById('schedule-edit-hour').value, 10);
  const minute = parseInt(document.getElementById('schedule-edit-minute').value, 10);
  closeScheduleEdit();
  postJSON('/api/pipelines/update', {service_id: sid, hour: hour, minute: minute},
           '已改 ' + sid + ' 定时时间为 ' +
           String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0') +
           '（≤30s 生效）');
}
function addPipeline(ev) {
  ev.preventDefault();
  const f = ev.target;
  const schedule = cronFromForm(f);
  if (!schedule) { alert('请填写自定义 cron 表达式（或改用预设）'); return; }
  postJSON('/api/pipelines/add', {
    service_id: f.service_id.value.trim(),
    kind: f.kind.value,
    category: f.category.value,
    schedule: schedule,
    enabled: f.enabled.checked,
    depends_on: f.depends_on.value.split(/[\\s,]+/).filter(Boolean),
    description: f.description.value.trim(),
  }, '定时任务已新增');
}
/* ---- 定时构建器（2026-10-10：预设+时点合成 cron，实时中文预览）--------- */
function cronFromForm(f) {
  const p = f.schedule_preset.value;
  const hh = String(parseInt(f.schedule_hour.value || '0', 10));
  const mm = String(parseInt(f.schedule_minute.value || '0', 10));
  if (p === 'daily') return mm + ' ' + hh + ' * * *';
  if (p === 'weekday') return mm + ' ' + hh + ' * * 1-5';
  if (p === 'weekly') return mm + ' ' + hh + ' * * ' + f.schedule_dow.value;
  if (p === 'monthly') return mm + ' ' + hh + ' ' +
    (parseInt(f.schedule_dom.value, 10) || 1) + ' * *';
  if (p === 'hourly') return '0 * * * *';
  return f.schedule.value.trim();
}
const _CRON_DOW_JS = {'0':'日','1':'一','2':'二','3':'三','4':'四','5':'五','6':'六','7':'日'};
/* 与服务端 _cron_zh 同形的预览子集（仅展示，不认识回退原文）。 */
function cronZh(cron) {
  const parts = (cron || '').trim().split(/\\s+/);
  if (parts.length !== 5) return null;
  const minute = parts[0], hour = parts[1], dom = parts[2],
        month = parts[3], dow = parts[4];
  if (month !== '*') return null;
  function times() {
    if (!/^\\d+$/.test(minute)) return null;
    const hours = hour.split(',');
    if (!hours.every(function (h) { return /^\\d+$/.test(h); })) return null;
    return hours.map(function (h) {
      return String(parseInt(h, 10)).padStart(2, '0') + ':' +
             String(parseInt(minute, 10)).padStart(2, '0');
    }).join('、');
  }
  if (dom === '*' && dow === '*') {
    if (minute.startsWith('*/') && hour === '*' && /^\\d+$/.test(minute.slice(2))) {
      return '每 ' + parseInt(minute.slice(2), 10) + ' 分钟';
    }
    if (hour === '*' && /^\\d+$/.test(minute)) {
      return '每小时第 ' + parseInt(minute, 10) + ' 分';
    }
    const t = times();
    return t ? '每天 ' + t : null;
  }
  if (dom === '*' && dow !== '*') {
    const t = times();
    if (dow === '1-5' && t) return '工作日（周一至周五）' + t;
    const days = dow.split(',');
    if (t && days.every(function (d) { return d in _CRON_DOW_JS; })) {
      return '每周' + days.map(function (d) { return _CRON_DOW_JS[d]; }).join('、周') + ' ' + t;
    }
    return null;
  }
  if (dom !== '*' && dow === '*' && /^\\d+$/.test(dom)) {
    const t = times();
    return t ? '每月 ' + parseInt(dom, 10) + ' 日 ' + t : null;
  }
  return null;
}
function updateSchedulePreview(f) {
  const cron = cronFromForm(f);
  const zh = cronZh(cron);
  const el = document.getElementById('schedule-preview');
  if (!el) return;
  el.textContent = zh ? ('将按：' + zh + '（' + cron + '）')
                      : (cron ? '将按 cron：' + cron : '');
}
function schedulePresetChange(f) {
  const p = f.schedule_preset.value;
  const isCustom = p === 'custom';
  const showTime = !isCustom && p !== 'hourly';
  f.schedule.style.display = isCustom ? '' : 'none';
  f.schedule_hour.style.display = showTime ? '' : 'none';
  f.schedule_minute.style.display = showTime ? '' : 'none';
  f.schedule_dow.style.display = p === 'weekly' ? '' : 'none';
  f.schedule_dom.style.display = p === 'monthly' ? '' : 'none';
  updateSchedulePreview(f);
}
function addRoster(ev) {
  ev.preventDefault();
  const f = ev.target;
  postJSON('/api/roster/add', {
    scope: f.scope.value,
    entity_type: f.entity_type.value,
    entity_key: f.entity_key.value.trim(),
    person_name: f.person_name.value.trim(),
    aliases: f.aliases.value.split(/[\\s,，、]+/).filter(Boolean),
    note: f.note.value.trim(),
    role: f.role.value,
  }, '名册已新增');
}
function setRosterStatus(id, value) {
  postJSON('/api/roster/toggle', {id: id, enabled: value === '1'},
           value === '1' ? '已启用' : '已停用');
}
function saveRegionTargets(scope, yearMonth) {
  const rows = [];
  document.querySelectorAll('input.person-target[data-scope="' + scope + '"]').forEach(function (el) {
    const raw = el.value.trim();
    rows.push({person_name: el.dataset.name, monthly_target: raw === '' ? null : Number(raw)});
  });
  postJSON('/api/roster/region-targets',
    {scope: scope, year_month: yearMonth, rows: rows}, '月目标已保存');
}
function deleteRoster(id) {
  if (!confirm('确认删除该名册记录？（审计会留存，日常建议用「停用」）')) return;
  postJSON('/api/roster/delete', {id: id}, '已删除（审计留存）');
}
/* ---- 工作日历页（2026-10-10，非技术同学操作面）-------------------------- */
function calendarToggle(day, isWork) {
  const action = isWork ? '改为休息' : '改为上班';
  if (!confirm('确认将 ' + day + ' ' + action + '？（当月日历立即重写）')) return;
  const note = prompt('备注（可空，写入审计与单元格提示）', '') || '';
  postJSON('/api/calendar/toggle',
    {date: day, is_workday: !isWork, note: note}, day + ' 已' + action);
}
function calendarImportYear(ev) {
  ev.preventDefault();
  const year = ev.target.year.value.trim();
  if (!confirm('确认导入 ' + year + ' 年法定节假日？（该年法定导入行整年替换，人工裁决不动；随后按当前口径重写全年日历）')) return;
  postJSON('/api/calendar/import-year', {year: Number(year)},
    year + ' 年法定假日已导入');
}
function calendarResetMonth(ym) {
  if (!confirm('确认清除 ' + ym + ' 的人工裁决（法定假日导入保留），恢复规则口径？')) return;
  postJSON('/api/calendar/reset-month', {month: ym}, ym + ' 已恢复规则口径');
}
function publishSnapshot() {
  if (!confirm('确认发布月度快照？（目标按当前输入覆盖 raw 快照，负责人按名册投影，随后自动触发提取重建榜单）')) return;
  const rows = [];
  document.querySelectorAll('input.roster-target').forEach(function (el) {
    const raw = el.value.trim();
    rows.push({
      store_name: el.dataset.store,
      channel: el.dataset.channel,
      monthly_target: raw === '' ? null : Number(raw),
    });
  });
  postJSON('/api/roster/publish-snapshot', {rows: rows},
    '月度快照已发布，提取重建已触发');
}
/* ---- 收件人覆盖抽屉（方案 B，2026-10-12：只改 @ 名单，不改正文）-------- */
let _recipientService = '';
function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function recipientClearMsg() {
  const el = document.getElementById('recipient-msg');
  if (el) { el.style.display = 'none'; el.textContent = ''; }
}
function recipientMsg(text) {
  const el = document.getElementById('recipient-msg');
  if (!el) return;
  el.textContent = text;
  el.style.display = 'block';
}
async function openRecipientOverride(serviceId) {
  _recipientService = serviceId;
  recipientClearMsg();
  document.getElementById('recipient-mask').style.display = 'flex';
  await loadRecipientOverride();
}
function closeRecipientOverride() {
  document.getElementById('recipient-mask').style.display = 'none';
}
async function loadRecipientOverride() {
  const el = document.getElementById('recipient-body');
  el.innerHTML = '<p class="hint">加载中…</p>';
  let data;
  try {
    const resp = await fetch('/api/pipelines/recipient-override?service_id=' +
      encodeURIComponent(_recipientService));
    if (!resp.ok) throw new Error(String(resp.status));
    data = await resp.json();
  } catch (err) {
    el.innerHTML = '<p class="flash err">读取异常，请稍后重试</p>';
    return;
  }
  renderRecipientOverride(data.overrides || []);
}
function renderRecipientOverride(rows) {
  const modeLabel = {include: '追加', exclude: '移除'};
  const typeLabel = {user_id: '账号', name: '姓名'};
  let trs = '';
  rows.forEach(function (o) {
    const disabled = o.enabled ? '' : ' <span class="hint">（已停用）</span>';
    trs += '<tr><td>' + (modeLabel[o.mode] || o.mode) + '</td><td>' +
      (typeLabel[o.target_type] || o.target_type) + '</td><td>' +
      esc(o.target_value) + '</td><td>' + esc(o.resolved_user_id || '—') +
      disabled + '</td><td>' + esc(o.note || '') + '</td><td>' +
      '<button type="button" onclick="recipientToggle(' + o.id + ',' +
      (o.enabled ? 'false' : 'true') + ')">' +
      (o.enabled ? '停用' : '启用') + '</button> ' +
      '<button type="button" onclick="recipientDelete(' + o.id +
      ')">删除</button></td></tr>';
  });
  const table = rows.length
    ? ('<table><tr><th>方向</th><th>目标类型</th><th>值</th><th>解析账号</th>' +
       '<th>备注</th><th>操作</th></tr>' + trs + '</table>')
    : '<p class="hint">暂无覆盖（当前按名册口径发送）</p>';
  document.getElementById('recipient-body').innerHTML =
    '<h2>收件人覆盖 · ' + esc(_recipientService) + '</h2>' +
    '<p class="hint">只改 @ 名单，不改消息正文；机器人在触发时套用。' +
    '「追加」加入、「移除」剔除；停用或删除即回退名册口径。姓名型需在' +
    '通讯录唯一命中才会 @（未命中会提示、不会 @ 错人）。</p>' + table +
    '<form class="inline" onsubmit="recipientAdd(event);return false">' +
    '<select name="mode"><option value="include">追加（加人）</option>' +
    '<option value="exclude">移除（去人）</option></select>' +
    '<select name="target_type"><option value="user_id">账号</option>' +
    '<option value="name">姓名</option></select>' +
    '<input name="target_value" placeholder="账号或姓名" required>' +
    '<input name="note" placeholder="备注（可空）">' +
    '<button type="submit">添加</button></form>' +
    '<div class="modal-btns"><button type="button" onclick="recipientReset()">' +
    '清空该线覆盖</button></div>';
}
async function recipientPost(url, body) {
  recipientClearMsg();
  let resp;
  try {
    resp = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)});
  } catch (err) {
    recipientMsg('操作失败：网络异常，请重试');
    return;
  }
  if (!resp.ok) {
    recipientMsg('操作失败（' + resp.status + '）：' +
      (_ERR_MSG[resp.status] || '请稍后重试'));
    return;
  }
  await loadRecipientOverride();
}
async function recipientAdd(ev) {
  ev.preventDefault();
  recipientClearMsg();
  const f = ev.target;
  const value = f.target_value.value.trim();
  if (!value) return;
  let resp;
  try {
    resp = await fetch('/api/pipelines/recipient-override/add', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        service_id: _recipientService, mode: f.mode.value,
        target_type: f.target_type.value, target_value: value,
        note: f.note.value.trim(),
      })});
  } catch (err) {
    recipientMsg('操作失败：网络异常，请重试');
    return;
  }
  if (!resp.ok) {
    recipientMsg('操作失败（' + resp.status + '）：' +
      (_ERR_MSG[resp.status] || '请稍后重试'));
    return;
  }
  const data = await resp.json();
  const unresolved = data.unresolved || [];
  if (unresolved.length) {
    recipientMsg('已保存，但 ' + unresolved.join('、') +
      ' 未在通讯录唯一命中，发送时会跳过（不会 @ 错人）');
  }
  await loadRecipientOverride();
}
function recipientToggle(id, enabled) {
  recipientPost('/api/pipelines/recipient-override/toggle',
    {id: id, enabled: enabled});
}
function recipientDelete(id) {
  if (!confirm('确认删除该条覆盖？（审计留存被删内容）')) return;
  recipientPost('/api/pipelines/recipient-override/delete', {id: id});
}
function recipientReset() {
  if (!confirm('确认清空 ' + _recipientService +
      ' 的全部覆盖，回退名册口径？')) return;
  recipientPost('/api/pipelines/recipient-override/reset',
    {service_id: _recipientService});
}
/* 定时构建器初始化（仅管道页存在该表单时生效）。 */
(function () {
  const f = document.getElementById('pipeline-add-form');
  if (f) schedulePresetChange(f);
})();
"""


def _page(title, *sections, viewer_name=""):
    who = f"当前管理员：{html.escape(viewer_name)}" if viewer_name else ""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{html.escape(title)} · ops-web</title>"
        f"<style>{_PAGE_STYLE}</style></head><body>"
        "<header><strong>ops-web</strong>"
        "<nav>"
        "<span class=\"nav-group\"><span class=\"nav-label\">日常</span>"
        "<a href=\"/roster\">填报名册</a>"
        "<a href=\"/calendar\">工作日历</a></span>"
        "<span class=\"nav-group\"><span class=\"nav-label\">权限</span>"
        "<a href=\"/\">成员与授权</a>"
        "<a href=\"/grants\">授权现状</a></span>"
        "<span class=\"nav-group\"><span class=\"nav-label\">系统</span>"
        "<a href=\"/pipelines\">定时任务</a></span>"
        "<span class=\"nav-group\"><span class=\"nav-label\">记录</span>"
        "<a href=\"/records\">操作记录</a></span>"
        "</nav>"
        "<a href=\"/auth/logout\">退出</a>"
        f"<span class=\"who\">{who}</span></header><main>"
        "<div id=\"flash\" class=\"flash\" style=\"display:none\"></div>"
        + "".join(sections)
        + f"</main><script>{_PAGE_JS}</script></body></html>"
    )


def _esc(value):
    return html.escape("" if value is None else str(value))


#: 北京时间（全库时间列约定存 UTC，展示层统一转北京时）。
_BJT = timezone(timedelta(hours=8))


def _current_year_month():
    """当前年月（北京时间，``YYYY-MM``）——区域月目标的键。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")


def _shift_year_month(year_month, months):
    """``YYYY-MM`` 平移 *months* 个月（名册页「下月」导航）。"""
    year, month = (int(part) for part in year_month.split("-"))
    index = year * 12 + (month - 1) + months
    return f"{index // 12:04d}-{index % 12 + 1:02d}"


def _fmt_time(value):
    """UTC 存储时间 → 北京时间字符串；None/认不出的原样返回。
    入参兼容 pymysql 返回的 datetime 与字符串两种形态；naive 一律按 UTC
    解读（与 sync_runs/robot_outbox 等表的存储约定一致）。
    """
    if value is None or value == "":
        return value
    dt = value if isinstance(value, datetime) else None
    if dt is None:
        text = str(value).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_BJT).strftime("%Y-%m-%d %H:%M:%S")


#: 定时任务页分表顺序（2026-10-10 运维裁决「按功能分表」：先业务动作
#: 后数据与平台；群名不分表——群是投递目标，管理群列已承载该视角）。
_CATEGORY_TABLE_ORDER = (
    "催办", "播报", "页面", "同步", "加工", "钉钉", "平台", "未归类",
)

# -- 展示层中文标签（底层值不动：审计/状态存量英文值仅在渲染时映射） --------
_RUN_STATUS_LABEL = {
    "finished": "成功",
    "failed": "失败",
    "rejected": "已拒绝",
    "pending": "等待调度",
    "launched": "运行中",
    "running": "运行中",
}
_TRIGGER_LABEL = {"cron": "定时", "run_once": "手动"}
_ACTION_LABEL = {
    "add": "新增", "enable": "启用", "disable": "停用",
    "delete": "删除", "seed": "种子导入", "publish": "发布快照",
    "update": "修正", "schedule": "改定时", "carry": "结转",
    "upsert": "改判", "reset": "恢复规则", "import": "法定导入",
    "reset_month": "月度恢复",
    "target": "月目标",
    "grant": "授权", "revoke": "收回",
}

#: 授权类型中文标签（成员与授权 / 授权现状 / 审计流水 三页共用）。
_GRANT_TYPE_LABEL = {"scope": "看板页面", "region": "区域数据"}

#: 名册 scope / entity_type 中文标签（填报名册页分组与表单）。
_SCOPE_LABEL = {
    "qudao": "渠道门店",
    "hangzhou": "杭州",
    "shaoxing": "绍兴",
    "junpin": "君品雅院",
    "vanke": "万科&大莲花&团购",
    "offline_all": "线下整体",
    "dining": "餐饮/部门",
}
_ENTITY_LABEL = {"store": "门店", "person": "人员", "dept": "部门"}

#: 名册角色中文标签（v3：负责人/代填报人；仅 store 类型有 deputy）。
_ROLE_LABEL = {"owner": "负责人", "deputy": "代填报人"}

#: 名册管理能力对应的 bi_authz scope 授权键（成员与授权页单人授权：
#: grant_type=scope、grant_key=roster；admin 隐式持有）。
ROSTER_MANAGE_SCOPE = "roster"


def _label(mapping, value):
    """注册值 → 中文标签；未注册的原样返回（渲染处仍过 _esc）。"""
    if value is None:
        return value
    return mapping.get(value, value)


#: 「操作记录」页的来源枚举（顺序即下拉顺序）。
_RECORD_SOURCES = ("授权", "定时任务", "填报名册", "工作日历", "收件人覆盖",
                   "定时运行")

#: 收件人覆盖 mode / target_type 中文标签（展示层）。
_MODE_LABEL = {"include": "追加", "exclude": "移除"}
_TARGET_TYPE_LABEL = {"user_id": "账号", "name": "姓名"}


def _audit_get(row, keys):
    """审计行取值：dict 按 key，序列按位置（与各页 unpack 顺序一致）。"""
    if isinstance(row, dict):
        return row.get
    return lambda key, r=row, ks=keys: r[ks.index(key)]


def _collect_records(connection):
    """5 处审计（授权/任务/名册/日历/收件人覆盖）+ 定时运行流水 → 统一行。

    每行形状 (时间, 来源, 操作人, 动作, 对象, 详情)，供「操作记录」页
    归一展示（任务3 2026-10-12：运维改完一处即可查到全部变更）。
    """
    records = []

    for row in bi_authz.fetch_audit_rows(connection):
        get = _audit_get(row, ("actor", "action", "user_id", "grant_type",
                               "grant_key", "note", "created_at"))
        records.append((
            get("created_at"), "授权", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            get("grant_key") or "",
            " ".join(x for x in (_label(_GRANT_TYPE_LABEL, get("grant_type")),
                                 get("note")) if x),
        ))

    for row in ops_control.fetch_pipeline_audit(connection):
        get = _audit_get(row, ("actor", "action", "service_id", "detail",
                               "created_at"))
        records.append((
            get("created_at"), "定时任务", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            get("service_id") or "", get("detail") or "",
        ))

    for row in report_roster.fetch_roster_audit(connection, limit=200):
        get = _audit_get(row, ("actor", "action", "scope", "entity_key",
                               "person_name", "detail", "created_at"))
        records.append((
            get("created_at"), "填报名册", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (_label(_SCOPE_LABEL, get("scope")),
                                 get("entity_key")) if x),
            " ".join(x for x in (get("person_name"), get("detail")) if x),
        ))

    for row in calendar_store.fetch_recent_audits(connection, limit=200):
        get = _audit_get(row, ("actor", "action", "business_date", "year",
                               "month", "detail", "created_at"))
        biz, yr, mon = get("business_date"), get("year"), get("month")
        when = (biz.isoformat() if isinstance(biz, date)
                else (f"{yr}-{int(mon):02d}" if yr and mon else "—"))
        records.append((
            get("created_at"), "工作日历", get("actor") or "",
            _label(_ACTION_LABEL, get("action")), when, get("detail") or "",
        ))

    # 覆盖审计是本期新表：未迁移/读失败只跳过该来源，不整页 503
    # （与 /pipelines 覆盖列的 fail-soft 口径一致）。
    try:
        override_audits = recipient_override.fetch_recent_audits(
            connection, limit=200)
    except Exception as exc:
        _LOGGER.warning("ops-web recipient override audit load failed: %s",
                        type(exc).__name__)
        override_audits = ()
    for row in override_audits:
        get = _audit_get(row, ("actor", "action", "service_id", "mode",
                               "target_type", "target_value", "detail",
                               "created_at"))
        target = " ".join(x for x in (
            _label(_MODE_LABEL, get("mode")),
            _label(_TARGET_TYPE_LABEL, get("target_type")),
            get("target_value")) if x)
        records.append((
            get("created_at"), "收件人覆盖", get("actor") or "",
            _label(_ACTION_LABEL, get("action")),
            " ".join(x for x in (get("service_id"), target) if x),
            get("detail") or "",
        ))

    for row in ops_control.fetch_run_history(connection):
        get = _audit_get(row, ("id", "service_id", "trigger_type", "status",
                               "exit_code", "note", "started_at",
                               "finished_at"))
        exit_code = get("exit_code")
        records.append((
            get("started_at"), "定时运行", "系统",
            _label(_RUN_STATUS_LABEL, get("status")),
            get("service_id") or "",
            " ".join(x for x in (
                _label(_TRIGGER_LABEL, get("trigger_type")),
                f"退出码 {exit_code}" if exit_code is not None else "",
                get("note")) if x),
        ))

    records.sort(key=lambda r: _fmt_time(r[0]), reverse=True)
    return records


#: 服务标识 → 中文说明（策展映射，展示层；注册表 description 真源不动）。
#: 未收录的 robot-/pages- 家族按后缀动态生成，其余回退注册表原文。
_SERVICE_LABELS = {
    "roll-manifest": "滚动源清单的 WDT 采集窗口（每轮同步前先把窗口往前推）",
    "sync-dingtalk": "钉钉 AI 表 → raw_dingtalk 同步",
    "sync-wdt": "旺店通读接口 → raw_wdt 同步",
    "project-mart": "raw → mart_ops 投影重建（参数化修复工具，不参与定时调度）",
    "extract-mart": "raw_dingtalk → mart_ops 提取（事实表 + 日历维表）",
    "sync-channel-sales": "渠道日销 T+1 补采（钉钉 AI 表渠道日销 + 月目标）",
    "extract-channel": "渠道日销 T+1 提取（门店事实表 + 目标，点名册依赖）",
    "channel-missing-check": "渠道门店到齐校验与催办（同步失败自动跳过不发）",
    "channel-daily-qudao": "渠道日报播报（已并入渠道榜单页，2026-09-23 停单独播报）",
    "pages-qudao-t1": "渠道榜单页 T+1 重算",
    "offline-daily-summary": "线下整体每日汇总（板块 + 日环比 + 月累计）",
    "offline-weekly-summary": "线下整体每周汇总（上周总量 + 周环比 + 排名）",
    "offline-monthly-summary": "线下整体每月汇总（上月总量 + 月环比 + 达成率排名）",
    "target-remind": "月目标核对提醒（每月 28 日 → 有目标的区域群，附名册页入口）",
    "target-rollover": "月目标自动结转（每月 1 日把上月目标幂等结转到当月，人工已录入不覆盖）",
    "calendar-watch": "工作日历覆盖看门（每月 25 日查当月/下月日历，缺失或规则兜底 → 线下整体群告警）",
    "dingtalk-gateway": "钉钉网关（outbox 投递 + 互动回调，常驻）",
    "kb-sync-products": "知识库商品同步（AI 表「产品资料」→ kb_products，小时级）",
    "kb-gateway": "知识库问答机器人（客服群，独立 Stream 应用，常驻）",
    "sync-runner": "旧版合并同步（双源，遗留入口）",
    "bi-web": "BI 看板（L1/L2，只读 mart_ops，常驻）",
    "scheduler": "调度器（按注册表定时触发各管道，自身无定时，常驻）",
    "ops-web": "运维台（权限管理 + 定时任务管理，常驻）",
}

_CRON_DOW = {
    "0": "日", "1": "一", "2": "二", "3": "三",
    "4": "四", "5": "五", "6": "六", "7": "日",
}

#: 区域 → 中文名（服务名/描述展示用；与 regions 注册表 display 对齐）。
_REGION_LABELS = {
    "hangzhou": "杭州",
    "vanke": "万科&大莲花&团购",
    "shaoxing": "绍兴",
    "junpin": "君品雅院",
    "qudao": "渠道",
    "offline_all": "线下整体",
}

#: 服务标识 → 中文名（策展映射，展示层；robot-/pages- 家族按区域动态生成）。
_SERVICE_NAMES = {
    "roll-manifest": "滚动清单",
    "sync-dingtalk": "钉钉 AI 表同步",
    "sync-wdt": "旺店通同步",
    "project-mart": "投影重建（修复工具）",
    "extract-mart": "提取加工",
    "sync-channel-sales": "渠道日销补采",
    "extract-channel": "渠道日销提取",
    "channel-missing-check": "渠道到齐校验催办",
    "channel-daily-qudao": "渠道日报播报",
    "pages-qudao-t1": "渠道榜单 T+1 重算",
    "offline-daily-summary": "线下每日汇总",
    "offline-weekly-summary": "线下每周汇总",
    "offline-monthly-summary": "线下每月汇总",
    "target-remind": "月目标核对提醒",
    "target-rollover": "月目标自动结转",
    "dingtalk-gateway": "钉钉网关",
    "kb-sync-products": "知识库商品同步",
    "kb-gateway": "知识库问答机器人",
    "sync-runner": "合并同步（旧）",
    "bi-web": "BI 看板",
    "scheduler": "调度器",
    "ops-web": "运维台",
}


def _service_name(service_id):
    """服务中文名：策展映射优先，robot-/pages-/leaderboard- 家族按区域生成。"""
    name = _SERVICE_NAMES.get(service_id)
    if name is not None:
        return name
    for prefix, tpl in (
        ("robot-check-", "「{region}」催办未填+DING"),
        ("robot-", "「{region}」填报提醒"),
        ("pages-", "「{region}」榜单页"),
        ("leaderboard-", "「{region}」榜单播报"),
    ):
        if service_id.startswith(prefix):
            region = service_id[len(prefix):]
            return tpl.format(region=_REGION_LABELS.get(region, region))
    return service_id


def _service_name_cell(service_id):
    """服务标识列：中文名为主、原始 id 小字备查；无中文名只显示 id。"""
    name = _service_name(service_id)
    if name == service_id:
        return _esc(service_id)
    return f"{_esc(name)}<span class=\"hint\">（{_esc(service_id)}）</span>"


def _service_label(service_id, description):
    """服务中文说明：策展映射优先，家族动态生成，兜底注册表原文。"""
    label = _SERVICE_LABELS.get(service_id)
    if label is not None:
        return label
    for prefix, tpl in (
        ("robot-check-", "「{region}」催办未填人 + DING（报数核对 → 群内 @ + DING）"),
        ("robot-", "「{region}」填报提醒（报数汇总 → 群内提醒）"),
        ("pages-", "「{region}」榜单页生成"),
        ("leaderboard-", "「{region}」榜单群播报（销售完成率榜 → 群）"),
    ):
        if service_id.startswith(prefix):
            region = service_id[len(prefix):]
            return tpl.format(region=_REGION_LABELS.get(region, region))
    return description


#: 功能分类已沉为注册表真源（PipelineConfig.category，七值与推导逻辑
#: 见 common.public_data.pipeline_taxonomy）；本页只保留展示层：
#: 真源优先、未配置的存量条目按 service_id 推导兜底。

#: 无 family 前缀但绑定区域的服务（「管理群」列用；pages-qudao-t1 的
#: pages- 后缀是 qudao-t1 不是区域，必须显式登记）。
_SERVICE_REGIONS = {
    "channel-daily-qudao": "qudao",
    "channel-missing-check": "qudao",
    "pages-qudao-t1": "qudao",
    "offline-daily-summary": "offline_all",
    "offline-weekly-summary": "offline_all",
    "offline-monthly-summary": "offline_all",
}

def _category_label(config):
    """功能列：注册表 category 真源优先，未配置按 service_id 推导兜底。"""
    return resolve_category(config.service_id, config.category) or "—"


def _service_region(service_id):
    """服务绑定区域：显式映射 → 家族后缀 → None（应用线/平台）。"""
    region = _SERVICE_REGIONS.get(service_id)
    if region is not None:
        return region
    for prefix, _ in CATEGORY_FAMILY:
        if service_id.startswith(prefix):
            return service_id[len(prefix):]
    return None


def _group_cell(service_id):
    """管理群列：区域线 → 「<区域中文名>」群；非区域线 → —。"""
    region = _service_region(service_id)
    if region is None:
        return "<span class=\"hint\">—</span>"
    return _esc(f"「{_REGION_LABELS.get(region, region)}」群")


#: 定时任务页「按管理群」视角的分组顺序（2026-10-12 运维裁决）：城市区域
#: 在前、跨区域线（渠道/线下整体）居中，无区域（平台/无群）恒置末位；
#: 未登记区域排在已知区域之后。分组键为区域 key（非中文 label）。
_GROUP_ORDER = tuple(_REGION_LABELS)


def _pipeline_group_title(region):
    """群视角分组头文案：区域 → 「<区域中文名>」群；None → 平台 / 无群。"""
    if region is None:
        return "平台 / 无群"
    return f"「{_REGION_LABELS.get(region, region)}」群"


def _category_bucket(config):
    """功能分表桶：注册表 category 真源优先、按标识推导兜底；None=未归类。"""
    return resolve_category(config.service_id, config.category) or "未归类"


def _targets_html(base, overrides, uid_to_name, scope_label=None,
                  store_count=None):
    """有效名单 + 口径徽标（2026-10-12 方案 B）。

    *base* 为基名单 ``[(user_id, name)]``，*overrides* 为该线启用覆盖行；
    有效名单 = ``base ∪ include − exclude``（与机器人侧同一
    :func:`recipient_override.apply_recipient_overrides`）。有覆盖时补
    ``（+N 覆盖／−M 移除）`` 徽标；口径徽标计数按有效名单。
    """
    effective = recipient_override.apply_recipient_overrides(base, overrides)
    display = dict(uid_to_name or {})
    for row in overrides:   # include 姓名型：写入快照即展示名
        if (str(row.get("mode") or "") == "include"
                and str(row.get("target_type") or "") == "name"
                and row.get("resolved_user_id")):
            display.setdefault(row["resolved_user_id"], row["target_value"])
    names = [
        display.get(uid) or (uid[1:] if uid.startswith("?") else uid)
        for uid in effective
    ]
    if not names:
        return "<span class=\"hint\">覆盖后无可@人</span>"
    base_ids = [uid for uid, _ in base]
    base_set, eff_set = set(base_ids), set(effective)
    added = [uid for uid in effective if uid not in base_set]
    removed = [uid for uid in base_ids if uid not in eff_set]
    meta = f"{len(names)} 人"
    meta += f" · {store_count} 店" if store_count is not None \
        else f" · {scope_label}"
    badge = ""
    if added or removed:
        bits = []
        if added:
            bits.append(f"+{len(added)} 覆盖")
        if removed:
            bits.append(f"−{len(removed)} 移除")
        badge = f"<span class=\"hint\">（{'／'.join(bits)}）</span>"
    return (f"{_esc('、'.join(names))}"
            f"<span class=\"hint\">（{meta}）</span>{badge}")


def _recipient_entry_html(service_id):
    """「收件人」入口：点开该线的任务级覆盖抽屉（方案 B，2026-10-12）。

    service_id 受注册表约束（字母数字/._:-）；此处再 ``_esc`` 兜底防注入。
    """
    return (f' <a href="#" class="recipient-entry" title="任务级收件人覆盖" '
            f"onclick=\"openRecipientOverride('{_esc(service_id)}');"
            f'return false">收件人…</a>')


def _reminder_targets_cell(service_id, *, members_by_region, roster_by_region,
                           qudao_owners, overrides=(), uid_to_name=None):
    """收受影响人列（仅催办行调用；其余行由调用方给 —）。

    口径与实际催办名单同源（2026-10-07 裁决「催办绑定名册」）：
    * ``robot-<区域>``（填报提醒）/ ``robot-check-<区域>``（催办+DING）
      = 该区域 ``dim_robot_member`` 在册填报人 ∩ 名册启用在册人员；
      名册无记录（fail-open）= 区域在册填报人全员，与机器人
      ``_unfilled`` 同真源；
    * 渠道线催办（channel-missing-check）= 名册店铺负责人
      （``dim_report_roster`` qudao/store/启用/role=owner 去重）；名册
      未登记时机器人回退月目标表 owners_json，此处如实标注不复制回退链。

    2026-10-12 方案 B：叠加任务级收件人覆盖（include/exclude），只改
    @ 名单；展示与机器人在触发时按同一 merge 口径，不承诺与某次运行逐位
    一致（页面只能给口径）。
    """
    entry = _recipient_entry_html(service_id)
    if service_id.startswith("robot-"):
        # _service_region 兼容 robot-check- 长者前缀（2026-10-07 拆分）。
        region = _service_region(service_id)
        pairs = members_by_region.get(region) or []
        roster = roster_by_region.get(region)
        if roster is not None:
            base = [(uid, name) for uid, name in pairs if name in roster]
            if not base:
                return ("<span class=\"hint\">名册在册但通讯录"
                        "无匹配成员</span>" + entry)
            return _targets_html(base, overrides, uid_to_name,
                                 scope_label="名册") + entry
        base = list(pairs)
        if not base:
            return "<span class=\"hint\">该区域无在册填报人</span>" + entry
        return _targets_html(base, overrides, uid_to_name,
                             scope_label="全员兜底") + entry
    if _service_region(service_id) == "qudao":
        owners = qudao_owners or {}
        names = sorted({name for bucket in owners.values() for name in bucket})
        if not names:
            return ("<span class=\"hint\">名册未登记店铺负责人"
                    "（机器人自动回退读门店月目标表）</span>" + entry)
        name_to_uid = {}
        for uid, name in (uid_to_name or {}).items():
            name_to_uid.setdefault(name, uid)
        base = [(name_to_uid.get(name) or ("?" + name), name)
                for name in names]
        return _targets_html(base, overrides, uid_to_name,
                             store_count=len(owners)) + entry
    return "<span class=\"hint\">—</span>"


def _cron_zh(schedule):
    """常见 cron 的人性化中文（仅展示；不认识的形态返回 None → 原文显示）。

    覆盖注册表全部形态：每天（多）时点、每周 X、每月 D 日、每 N 分钟、
    每小时第 M 分；其余（含月份字段）一律回退原文。
    """
    parts = schedule.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if month != "*":
        return None

    def _times():
        if not minute.isdigit():
            return None
        hours = hour.split(",")
        if not all(h.isdigit() for h in hours):
            return None
        return "、".join(f"{int(h):02d}:{int(minute):02d}" for h in hours)

    if dom == "*" and dow == "*":
        if minute.startswith("*/") and hour == "*" and minute[2:].isdigit():
            return f"每 {int(minute[2:])} 分钟"
        if hour == "*" and minute.isdigit():
            return f"每小时第 {int(minute)} 分"
        times = _times()
        return f"每天 {times}" if times else None
    if dom == "*" and dow != "*":
        times = _times()
        # 工作日区间（定时构建器「工作日」预设与存量注册表常见形态）
        if dow == "1-5" and times:
            return f"工作日（周一至周五）{times}"
        days = dow.split(",")
        if times and all(d in _CRON_DOW for d in days):
            return "每周" + "、周".join(_CRON_DOW[d] for d in days) + f" {times}"
        return None
    if dom != "*" and dow == "*" and dom.isdigit():
        times = _times()
        return f"每月 {int(dom)} 日 {times}" if times else None
    return None


def _schedule_cell(schedule):
    """定时规则列：中文人性化为主、原表达式作小字备查；无定时=常驻。"""
    if not schedule:
        return "<span class=\"hint\">常驻</span>"
    zh = _cron_zh(schedule)
    if zh is None:
        return _esc(schedule)
    return f"{_esc(zh)}<span class=\"hint\">（{_esc(schedule)}）</span>"


def _link_name_html(row):
    """链接的姓名单元（别名折叠为小字）。"""
    name_html = _esc(row["person_name"])
    if row.get("aliases"):
        try:
            alias_text = "、".join(json.loads(row["aliases"]))
        except (ValueError, TypeError):
            alias_text = ""
        if alias_text:
            name_html += f"<span class=\"hint\">（{_esc(alias_text)}）</span>"
    return name_html


def _status_select_html(row):
    """链接的状态下拉（2026-10-07 运维裁决：下拉直接改状态，当前值即状态）。"""
    enabled = bool(row["enabled"])
    return (
        f"<select onchange=\"setRosterStatus({int(row['id'])}, this.value)\">"
        f"<option value=\"1\"{' selected' if enabled else ''}>启用</option>"
        f"<option value=\"0\"{'' if enabled else ' selected'}>停用</option>"
        f"</select>"
    )


def _delete_btn_html(row):
    return f"<button onclick=\"deleteRoster({int(row['id'])})\">删除</button>"


#: 渠道门店表可排序列（2026-10-07 运维裁决）：?sort= 白名单，非法值回退默认。
_QUDAO_SORTS = ("channel", "store", "owner", "deputy", "target")


def _qudao_section(rows, targets, numbers, sort="channel"):
    """渠道门店一体视图（S2）：目标可编辑 + 负责人名册 + 发布快照。

    统一模板（2026-10-07 运维裁决）：行 = 负责人链接，列为
    渠道/地区▾ | 店铺/对象 | 月目标（元）| 负责人 | 代填报人 | 状态 | 备注 | 更新 | 操作
    ——聚合列（渠道/店铺/月目标/代填报人）靠左、链接列靠右。代填报人
    不单独占行：聚进店铺级代填报人格（同店铺格 rowspan，逐人带状态
    下拉+删除，不显示备注）；单负责人店铺整行无聚合。排序：五列点
    表头；``sort=channel``（默认）时渠道单元格同样合并、组内按编号
    升序。目标列输入框即「草稿」，点「发布月度快照」才落 raw
    （publishSnapshot JS 收集）。
    """
    store_rows = {}
    for row in rows:
        store_rows.setdefault(row["entity_key"], []).append(row)
    all_stores = sorted(set(store_rows) | set(targets.keys()))
    metas = {}
    for store in all_stores:
        channel, target = targets.get(
            store, (numbers.get(store, ("", None))[0], None)
        )
        links = store_rows.get(store, [])
        metas[store] = {
            "channel": channel,
            "target": target,
            "store_no": numbers.get(store, (None, None))[1],
            "owners": sorted(r["person_name"] for r in links
                             if r.get("role") != "deputy"),
            "deputies": sorted(r["person_name"] for r in links
                               if r.get("role") == "deputy"),
            # 链接行序：负责人在前、代填报人在后，各自按姓名
            "links": sorted(
                links,
                key=lambda r: (1 if r.get("role") == "deputy" else 0,
                               r["person_name"], int(r["id"])),
            ),
        }

    def _channel_rank(channel):
        if channel in CHANNELS:
            return (0, CHANNELS.index(channel))
        return (1, channel or "")

    if sort == "store":
        ordered = all_stores  # 店名升序
    elif sort == "owner":
        ordered = sorted(
            all_stores,
            key=lambda s: ((0, metas[s]["owners"][0])
                           if metas[s]["owners"] else (1, ""), s),
        )
    elif sort == "deputy":
        ordered = sorted(
            all_stores,
            key=lambda s: ((0, metas[s]["deputies"][0])
                           if metas[s]["deputies"] else (1, ""), s),
        )
    elif sort == "target":
        ordered = sorted(
            all_stores,
            key=lambda s: ((0, -float(metas[s]["target"]))
                           if metas[s]["target"] else (1, 0.0), s),
        )
    else:  # channel（默认）：渠道聚合，组内编号升序
        ordered = sorted(
            all_stores,
            key=lambda s: (_channel_rank(metas[s]["channel"]),
                           metas[s]["store_no"] or 10 ** 6, s),
        )

    def _th(label, key):
        mark = " ▾" if sort == key else ""
        return f"<th><a href=\"?sort={key}\">{_esc(label)}{mark}</a></th>"

    # 展开为负责人链接行（2026-10-07 运维裁决）：代填报人不单独占行——
    # 聚进店铺级「代填报人」格（与店铺/月目标格同 rowspan，格内逐人带
    # 状态下拉+删除，不显示备注）；无负责人的店保留一行（目标仍可编辑）。
    # 单负责人店铺因此整行无聚合。
    flat = []
    for store in ordered:
        meta = metas[store]
        meta["owner_links"] = [l for l in meta["links"]
                               if l.get("role") != "deputy"]
        meta["deputy_links"] = [l for l in meta["links"]
                                if l.get("role") == "deputy"]
        for link in meta["owner_links"] or [None]:
            flat.append((store, meta, link))

    body = []
    total = 0.0
    channel_span_left = 0
    store_span_left = 0
    for position, (store, meta, link) in enumerate(flat):
        cells = []
        if sort == "channel":
            if channel_span_left == 0:
                span = 1
                for nxt_store, nxt_meta, _link in flat[position + 1:]:
                    if nxt_meta["channel"] == meta["channel"]:
                        span += 1
                    else:
                        break
                channel_span_left = span
                channel_text = _esc(meta["channel"]) or "—"
                cells.append(
                    f"<td rowspan=\"{span}\">{channel_text}</td>"
                    if span > 1 else f"<td>{channel_text}</td>"
                )
            channel_span_left -= 1
        else:
            cells.append(f"<td>{_esc(meta['channel']) or '—'}</td>")
        first_of_store = store_span_left == 0
        if first_of_store:
            store_span = len(meta["owner_links"]) or 1
            store_span_left = store_span
            target = meta["target"]
            if target:
                total += float(target)
            store_cell = (
                f"<td rowspan=\"{store_span}\">{_esc(store)}</td>"
                if store_span > 1 else f"<td>{_esc(store)}</td>"
            )
            target_value = "" if target is None else f"{float(target):.0f}"
            # 月目标格参与店铺聚合（与店铺格同 rowspan，紧随店铺格）——
            # 聚合列（渠道/店铺/月目标）靠左、链接列靠右（2026-10-07 裁决）。
            rowspan = f' rowspan="{store_span}"' if store_span > 1 else ""
            target_cell = (
                f"<td{rowspan}><input class=\"roster-target\" "
                f"data-store=\"{_esc(store)}\" "
                f"data-channel=\"{_esc(meta['channel'])}\" "
                f"value=\"{_esc(target_value)}\" "
                f"placeholder=\"目标（元）\"></td>"
            )
            cells.append(store_cell + target_cell)
        store_span_left -= 1
        if link is None:
            cells.append("<td><span class=\"hint\">无负责人在册</span></td>")
        else:
            cells.append(f"<td>{_link_name_html(link)}</td>")
        if first_of_store:
            # 代填报人店铺格（2026-10-07 运维裁决）：只读展示——不带状态
            # 下拉与删除（代填授权不在此办理），不显示备注。
            deputy_html = "".join(
                f"<div>{_link_name_html(l)}</div>"
                for l in meta["deputy_links"]
            ) or "<span class=\"hint\">—</span>"
            rowspan = f' rowspan="{store_span}"' if store_span > 1 else ""
            cells.append(f"<td{rowspan}>{deputy_html}</td>")
        if link is None:
            cells.append("<td></td><td></td><td></td><td></td>")
        else:
            updated = _fmt_time(link.get("updated_at")) or "—"
            cells.append(
                f"<td>{_status_select_html(link)}</td>"
                f"<td>{_esc(link.get('note'))}</td>"
                f"<td><span class=\"hint\">{_esc(updated)}</span></td>"
                f"<td>{_delete_btn_html(link)}</td>"
            )
        body.append("<tr>" + "".join(cells) + "</tr>")
    return (
        "<h2>渠道门店<span class=\"hint\">（qudao，目标 + 负责人一体管理；"
        "点表头排序，默认按渠道聚合）</span></h2>"
        "<table><tr>"
        + _th("渠道/地区", "channel")
        + _th("店铺/对象", "store")
        + _th("月目标（元）", "target")
        + _th("负责人", "owner")
        + _th("代填报人", "deputy")
        + "<th>状态</th><th>备注</th><th>更新</th><th>操作</th></tr>"
        + ("".join(body)
           or "<tr><td colspan=\"9\" class=\"hint\">暂无记录</td></tr>")
        + "</table>"
        f"<p>合计：<strong>{total:.0f}</strong> 元 "
        "<button onclick=\"publishSnapshot()\">发布月度快照</button>"
        "<span class=\"hint\">（负责人按名册投影写 raw 快照，自动触发提取重建榜单；"
        "改动未发布前不落库）</span></p>"
    )


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------


def create_app(*, settings, session_secret, db_connector=None, auth_client=None,
               viewer_resolver=None, session_secure=False,
               fleet_source=None, config_source=None,
               config_publisher=None, corp_id=None, agent_id=None) -> FastAPI:
    """装配 ops-web；``session_secret`` 必填（ops-web 没有开放模式）。

    依赖全部可注入，测试不需要真实库与网络：``db_connector`` 喂
    FakeConnection，``viewer_resolver`` 喂静态 Viewer，``auth_client``
    喂 fake 交换客户端。定时任务管理面另有三注入：``fleet_source``
    （零参 callable → service_id 元组）、``config_source``（注册表读）、
    ``config_publisher``（注册表写，None = 写 API 降级 503）。
    """
    if not session_secret:
        raise ValueError("ops-web requires a session secret")
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if viewer_resolver is None:
        viewer_resolver = authz.ViewerResolver(db_connector)

    app = FastAPI(title="ops-web")

    def _resolve_request_viewer(request: Request) -> authz.Viewer:
        """session → Viewer（各闸共用的解析段；未登录 401/302，解析异常 403）。"""
        raw = request.cookies.get(auth.SESSION_COOKIE)
        userid = (
            auth.resolve_session_userid(raw, session_secret) if raw else None
        )
        if userid is None:
            accept = request.headers.get("accept", "")
            if "application/json" in accept or request.url.path.startswith("/api/"):
                raise HTTPException(status_code=401, detail=ErrorDetail.UNAUTHORIZED)
            raise HTTPException(
                status_code=302, headers={"Location": "/auth/entry?reason=login"}
            )
        try:
            return viewer_resolver.resolve(userid)
        except Exception as exc:
            # 权限链 fail-closed（铁律 2）：解析异常 = 拒绝。
            _LOGGER.warning("ops-web viewer resolve failed: %s", type(exc).__name__)
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)

    def _forbidden(request: Request):
        if request.url.path.startswith("/api/"):
            raise HTTPException(status_code=403, detail=ErrorDetail.FORBIDDEN)
        raise HTTPException(
            status_code=302,
            headers={"Location": "/auth/entry?reason=forbidden"},
        )

    def require_admin(request: Request) -> authz.Viewer:
        """应用层闸：session 有效 + admin；其余一律提示页/403。"""
        viewer = _resolve_request_viewer(request)
        if not viewer.is_admin:
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    def require_roster_manager(request: Request) -> authz.Viewer:
        """名册管理闸（2026-10-07 运维裁决）：admin 或持 ``scope='roster'``
        授权（成员与授权页单人授权分配）——名册的新增/删除/状态修改绑定
        该授权，不再是 admin 的隐式全集。"""
        viewer = _resolve_request_viewer(request)
        if not (viewer.is_admin or ROSTER_MANAGE_SCOPE in viewer.scopes):
            _forbidden(request)
        request.state.viewer = viewer
        return viewer

    # -- 认证路由（与 bi-web 同一套实现，铁律 7）----------------------------
    @app.post("/auth/dingtalk")
    async def auth_dingtalk(request: Request):
        if auth_client is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            payload = await request.json()
        except Exception:
            payload = None
        auth_code = payload.get("authCode") if isinstance(payload, dict) else None
        if not isinstance(auth_code, str) or not auth_code:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            userid = auth_client.exchange_auth_code(auth_code)
        except auth.AuthError as exc:
            _LOGGER.warning("ops-web auth exchange failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        _LOGGER.info("ops-web login: %s", auth.mask_userid(userid))
        response = JSONResponse({"status": "ok"})
        response.set_cookie(
            auth.SESSION_COOKIE,
            auth.issue_session(userid, session_secret),
            max_age=auth.SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=session_secure,
        )
        return response

    @app.get("/auth/entry")
    def auth_entry():
        return FileResponse(auth.AUTH_ENTRY_PAGE)

    @app.get("/auth/logout")
    def auth_logout():
        response = RedirectResponse("/auth/entry?reason=loggedout")
        response.delete_cookie(auth.SESSION_COOKIE)
        return response

    @app.get("/auth/jsapi-config")
    def auth_jsapi_config(url: str = ""):
        # dd.config 签名原料（与 bi-web 同款，设计稿 §5.3）：agentId/corpId +
        # 随机 nonceStr/timeStamp + 对当前页 URL 的 SHA1 签名。与 /auth/dingtalk
        # 同属认证路由；url 仅参与签名、不回显，scheme 非 http/https 直接 400。
        if auth_client is None or not corp_id or not agent_id:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        nonce = auth.make_nonce()
        timestamp = int(time.time() * 1000)
        try:
            ticket = auth_client.jsapi_ticket()
        except auth.AuthError as exc:
            _LOGGER.warning(
                "ops-web jsapi ticket fetch failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse(
            {
                "agentId": agent_id,
                "corpId": corp_id,
                "timeStamp": timestamp,
                "nonceStr": nonce,
                "signature": auth.build_jsapi_signature(
                    ticket, nonce, timestamp, url
                ),
            }
        )

    @app.get("/healthz")
    def healthz():
        # 顶层运维端点（与 bi-web 同款）：活性与 mart 连通性（SELECT 1）。
        try:
            with db_connector() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                finally:
                    cursor.close()
        except Exception as exc:
            _LOGGER.warning(
                "ops-web healthz mart probe failed: %s", type(exc).__name__
            )
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    # -- 页面 ----------------------------------------------------------------
    @app.get("/")
    def members_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web members load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = "".join(
            f"<tr><td>{_esc(user_id)}</td><td>{_esc(name)}</td>"
            f"<td>{_esc(region)}</td><td>{_esc(dept_name)}</td></tr>"
            for user_id, name, region, dept_name in members
        )
        regions = sorted({region for _uid, _n, region, _d in members})
        region_options = "".join(
            f"<option value=\"{_esc(region)}\">{_esc(region)}</option>"
            for region in regions
        )
        grant_form = (
            "<h2>单人授权</h2>"
            "<form class=\"inline\" onsubmit=\"submitGrant(event)\">"
            "<input name=\"user_id\" placeholder=\"账号\" required>"
            "<select name=\"grant_type\">"
            "<option value=\"scope\">看板页面</option>"
            "<option value=\"region\">区域数据</option>"
            "</select>"
            "<input name=\"grant_key\" placeholder=\"对象：看板名或区域名\" required>"
            "<input name=\"note\" placeholder=\"备注\">"
            "<button type=\"submit\">授权</button></form>"
            # G3/G6/G4（2026-10-12 断点报告）：两套体系显性化 + scope=roster
            # 引导（原说明只写在需授权才能看到的名册页，引导死锁）+ 生效延迟。
            "<p class=\"hint\">本页授权管 BI 看板与本中心的访问权、以及名册"
            "管理权（类型选「看板页面」、对象填 roster（名册管理））；「谁能填报/被催办」由"
            "「填报名册」页维护，与这里互不影响——机器人门禁实时读名册，"
            "名册改完即时生效。本页授权约 1 分钟内生效。</p>"
            "<h2>按区域一键开通</h2>"
            "<form class=\"inline\" onsubmit=\"grantRegion(event)\">"
            f"<select name=\"region\">{region_options}</select>"
            "<button type=\"submit\">该区域全员开通区域权限</button>"
            "<span class=\"hint\">候选名单即下表在职成员；逐行落审计。</span>"
            "</form>"
        )
        table = (
            "<h1>在职成员</h1>"
            "<table><tr><th>账号</th><th>姓名</th><th>区域</th><th>部门</th></tr>"
            f"{rows}</table>"
        )
        return HTMLResponse(_page("成员与授权", table, grant_form,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/grants")
    def grants_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                grants = bi_authz.fetch_all_grants(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web grants load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in grants:
            get = (lambda key: row.get(key)) if isinstance(row, dict) else None
            if get is not None:
                user_id, grant_type = get("user_id"), get("grant_type")
                grant_key, note = get("grant_key"), get("note")
                updated_by, updated_at = get("updated_by"), get("updated_at")
            else:
                (user_id, grant_type, grant_key, note,
                 updated_by, updated_at) = row
            rows.append(
                f"<tr><td>{_esc(user_id)}</td>"
                f"<td>{_esc(_label(_GRANT_TYPE_LABEL, grant_type))}</td>"
                f"<td>{_esc(grant_key)}</td><td>{_esc(note)}</td>"
                f"<td>{_esc(updated_by)}</td><td>{_esc(updated_at)}</td>"
                f"<td><button onclick=\"revokeGrant('{_esc(user_id)}',"
                f"'{_esc(grant_type)}','{_esc(grant_key)}')\">收回</button></td></tr>"
            )
        table = (
            "<h1>授权现状</h1>"
            "<table><tr><th>账号</th><th>类型</th><th>对象</th><th>备注</th>"
            "<th>最后操作人</th><th>更新时间</th><th></th></tr>"
            + "".join(rows) + "</table>"
        )
        return HTMLResponse(_page("授权现状", table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/audit")
    def audit_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                entries = bi_authz.fetch_audit_rows(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web audit load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in entries:
            if isinstance(row, dict):
                values = (row.get("actor"), row.get("action"), row.get("user_id"),
                          row.get("grant_type"), row.get("grant_key"),
                          row.get("note"), row.get("created_at"))
            else:
                values = row
            actor, action, user_id, grant_type, grant_key, note, created_at = values
            rows.append(
                f"<tr><td>{_esc(created_at)}</td><td>{_esc(actor)}</td>"
                f"<td>{_esc(_label(_ACTION_LABEL, action))}</td>"
                f"<td>{_esc(user_id)}</td>"
                f"<td>{_esc(_label(_GRANT_TYPE_LABEL, grant_type))}</td>"
                f"<td>{_esc(grant_key)}</td>"
                f"<td>{_esc(note)}</td></tr>"
            )
        table = (
            "<h1>审计流水</h1>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th><th>账号</th>"
            "<th>类型</th><th>对象</th><th>备注</th></tr>"
            + "".join(rows) + "</table>"
        )
        return HTMLResponse(_page("审计流水", table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/records")
    def records_page(request: Request):
        """统一「操作记录」：5 处分散审计（授权/任务/名册/日历/收件人覆盖）
        + 定时运行流水归一到一页，按来源/操作人/动作/起始日期筛选
        （任务3 2026-10-12）。"""
        viewer = require_admin(request)
        source_q = (request.query_params.get("source") or "").strip()
        actor_q = (request.query_params.get("actor") or "").strip()
        action_q = (request.query_params.get("action") or "").strip()
        since_q = (request.query_params.get("since") or "").strip()
        if source_q not in _RECORD_SOURCES:
            source_q = ""
        try:
            with db_connector() as connection:
                rows = _collect_records(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web records load failed: %s",
                            type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

        if source_q:
            rows = [r for r in rows if r[1] == source_q]
        if actor_q:
            rows = [r for r in rows if actor_q in (r[2] or "")]
        if action_q:
            rows = [r for r in rows if action_q in (r[3] or "")]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", since_q):
            rows = [r for r in rows if _fmt_time(r[0])[:10] >= since_q]
        rows = rows[:200]

        options = "".join(
            f"<option value=\"{_esc(s)}\""
            f"{' selected' if s == source_q else ''}>{_esc(s)}</option>"
            for s in _RECORD_SOURCES
        )
        form = (
            "<form class=\"inline\" method=\"get\" action=\"/records\">"
            "<select name=\"source\"><option value=\"\">全部来源</option>"
            + options + "</select>"
            f"<input name=\"actor\" placeholder=\"操作人（可空）\" "
            f"value=\"{_esc(actor_q)}\">"
            f"<input name=\"action\" placeholder=\"动作（可空）\" "
            f"value=\"{_esc(action_q)}\">"
            f"<input name=\"since\" type=\"date\" value=\"{_esc(since_q)}\">"
            "<button type=\"submit\">筛选</button>"
            "<a href=\"/records\" style=\"margin-left:8px\">重置</a></form>"
        )
        body_rows = "".join(
            f"<tr><td>{_esc(_fmt_time(r[0]))}</td><td>{_esc(r[1])}</td>"
            f"<td>{_esc(r[2])}</td><td>{_esc(r[3])}</td>"
            f"<td>{_esc(r[4])}</td><td>{_esc(r[5])}</td></tr>"
            for r in rows
        )
        table = (
            "<h1>操作记录</h1>"
            + form
            + "<table><tr><th>时间</th><th>来源</th><th>操作人</th><th>动作</th>"
            "<th>对象</th><th>详情</th></tr>"
            + (body_rows
               or "<tr><td colspan=\"6\" class=\"hint\">暂无记录</td></tr>")
            + "</table>"
            "<p class=\"hint\">汇总授权 / 定时任务变更 / 填报名册 / 工作日历"
            "/ 收件人覆盖五类人工操作，加定时运行流水；按时间倒序，最多 200 条。"
            "时间为北京时间。</p>"
        )
        return HTMLResponse(_page("操作记录", table,
                                  viewer_name=viewer.name or viewer.userid))

    # -- 写 API（事务内两写：grant + audit；actor 来自 session）--------------
    @app.post("/api/grants")
    async def grant_create(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        try:
            record = bi_authz.validate_grant_fields(
                payload.get("user_id"),
                payload.get("grant_type"),
                payload.get("grant_key"),
                payload.get("note", ""),
            )
        except bi_authz.BiAuthzError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                bi_authz.upsert_grant(connection, record, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning("ops-web grant write failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/grants/delete")
    async def grant_delete(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        user_id = payload.get("user_id")
        grant_type = payload.get("grant_type")
        grant_key = payload.get("grant_key")
        if (not isinstance(user_id, str) or not user_id
                or grant_type not in bi_authz.GRANT_TYPES
                or not isinstance(grant_key, str) or not grant_key):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                deleted = bi_authz.delete_grant(
                    connection, user_id, grant_type, grant_key,
                    actor=viewer.userid,
                )
        except Exception as exc:
            _LOGGER.warning("ops-web grant delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    @app.post("/api/grants/region")
    async def grant_region(request: Request):
        """按区域一键开通：候选名单读 dim_robot_member，逐行 upsert + audit。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        region = payload.get("region")
        if not isinstance(region, str) or not region.strip():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        region = region.strip()
        try:
            with db_connector() as connection:
                members = bi_authz.fetch_active_members(connection)
                written = 0
                for user_id, _name, member_region, _dept in members:
                    if member_region != region:
                        continue
                    bi_authz.upsert_grant(
                        connection,
                        bi_authz.GrantRecord(
                            user_id, "region", region, f"按区域批量开通（{region}）"
                        ),
                        actor=viewer.userid,
                    )
                    written += 1
        except Exception as exc:
            _LOGGER.warning("ops-web region grant failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "written": written})

    # -- 定时任务管理（管道注册表 + 立即运行一次）----------------------------

    def _fleet():
        """舰队清单；未接线或读取失败 -> 503（页面与写 API 共用）。"""
        if fleet_source is None or config_source is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            return tuple(fleet_source())
        except Exception as exc:
            _LOGGER.warning("ops-web fleet load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

    @app.get("/pipelines")
    def pipelines_page(request: Request):
        viewer = require_admin(request)
        # 双视角（2026-10-12 运维裁决）：默认「按管理群」（同群任务聚合，
        # 一眼看清这个群会收到哪些任务）；?view=class 走原「按功能类别」。
        view = (request.query_params.get("view") or "group").strip()
        if view not in ("group", "class"):
            view = "group"
        service_ids = _fleet()
        configs = []
        for service_id in service_ids:
            try:
                configs.append(config_source.get_pipeline(service_id))
            except Exception as exc:
                # 单条注册表项畸形不拖垮整页：按默认配置展示。
                _LOGGER.warning(
                    "ops-web pipeline config parse failed: %s", type(exc).__name__
                )
                configs.append(PipelineConfig(service_id))
        try:
            with db_connector() as connection:
                recent = ops_control.fetch_recent_requests(connection, limit=100)
        except Exception as exc:
            _LOGGER.warning("ops-web run requests load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        latest = {}
        for row in recent:  # fetch 按 id DESC，首见即最新
            service_id = row.get("service_id") if isinstance(row, dict) else row[1]
            if service_id not in latest:
                latest[service_id] = row
        # 收受影响人（催办行）：附属信息 fail-soft——名册/通讯录读取故障
        # 只影响本列，不拖垮整页（注册表才是本页真源）。
        reminder_cells = {}
        reminder_ids = [
            config.service_id for config in configs
            if resolve_category(config.service_id, config.category) == "催办"
        ]
        if reminder_ids:
            members_by_region = {}
            roster_by_region = {}
            uid_to_name = {}
            overrides_by_service = {}
            qudao_owners = None
            try:
                with db_connector() as connection:
                    robot_regions = {
                        _service_region(sid) for sid in reminder_ids
                        if sid.startswith("robot-")
                    }
                    if robot_regions:
                        for uid, name, region, _dept in \
                                bi_authz.fetch_active_members(connection):
                            members_by_region.setdefault(region, []).append(
                                (uid, name))
                            uid_to_name.setdefault(uid, name)
                        for region in robot_regions:
                            roster_by_region[region] = \
                                report_roster.fetch_scope_person_names(
                                    connection, region)
                    if any(not sid.startswith("robot-")
                           and _service_region(sid) == "qudao"
                           for sid in reminder_ids):
                        qudao_owners = report_roster.fetch_store_owner_map(
                            connection, "qudao", roles=("owner",))
                    for sid in reminder_ids:
                        # 覆盖表是本期新表：未迁移/读失败只回退名册口径，
                        # 不让整列「读取失败」。
                        try:
                            overrides_by_service[sid] = \
                                recipient_override.fetch_overrides(
                                    connection, sid)
                        except Exception as exc:
                            _LOGGER.warning(
                                "ops-web recipient override load failed: %s",
                                type(exc).__name__)
                            overrides_by_service[sid] = ()
                for sid in reminder_ids:
                    reminder_cells[sid] = _reminder_targets_cell(
                        sid, members_by_region=members_by_region,
                        roster_by_region=roster_by_region,
                        qudao_owners=qudao_owners,
                        overrides=overrides_by_service.get(sid, ()),
                        uid_to_name=uid_to_name)
            except Exception as exc:
                _LOGGER.warning(
                    "ops-web reminder targets load failed: %s",
                    type(exc).__name__)
                for sid in reminder_ids:
                    reminder_cells[sid] = (
                        "<span class=\"hint\">读取失败</span>")
        # 桶：群视角按区域 key（None=平台/无群）；功能视角按功能分类。
        buckets = ({}
                   if view == "group"
                   else {title: [] for title in _CATEGORY_TABLE_ORDER})
        for config in configs:
            request_row = latest.get(config.service_id)
            if request_row is None:
                request_cell = "—"
            else:
                get = (
                    (lambda key: request_row.get(key))
                    if isinstance(request_row, dict)
                    else (lambda key: request_row[
                        ("id", "service_id", "requested_by", "status", "note",
                         "exit_code", "created_at", "finished_at").index(key)])
                )
                request_cell = (
                    f"{_esc(_label(_RUN_STATUS_LABEL, get('status')))}"
                    f"<span class=\"hint\">（{_esc(get('requested_by'))} "
                    f"{_esc(_fmt_time(get('created_at')))}）</span>"
                )
            template = (
                "✓" if has_command(config.service_id)
                else "<span class=\"hint\">无模板</span>"
            )
            toggle_label = "停用" if config.enabled else "启用"
            run_option = (
                "<option value=\"runonce\">运行一次</option>"
                if has_command(config.service_id) else ""
            )
            action_cell = (
                f"<select data-service=\"{_esc(config.service_id)}\" "
                f"data-enabled=\"{'1' if config.enabled else '0'}\" "
                f"data-schedule=\"{_esc(config.schedule or '')}\" "
                "onchange=\"pipelineAction(this)\">"
                "<option value=\"\">操作…</option>"
                f"<option value=\"toggle\">{toggle_label}</option>"
                "<option value=\"schedule\">改定时时间</option>"
                f"{run_option}</select>"
            )
            # 视角差异仅在「管理群」列：群视角该维度已升为分组头，行内不再重复。
            head = f"<tr><td>{_service_name_cell(config.service_id)}</td>"
            if view != "group":
                head += f"<td>{_group_cell(config.service_id)}</td>"
            row = (
                head
                + f"<td>{reminder_cells.get(config.service_id, '<span class=\"hint\">—</span>')}</td>"
                f"<td>{'✓' if config.enabled else '—'}</td>"
                f"<td>{_schedule_cell(config.schedule)}</td>"
                f"<td>{_esc(', '.join(config.depends_on))}</td>"
                f"<td>{_esc(_service_label(config.service_id, config.description))}</td>"
                f"<td>{template}</td>"
                f"<td>{request_cell}</td>"
                f"<td>{action_cell}</td></tr>"
            )
            if view == "group":
                buckets.setdefault(
                    _service_region(config.service_id), []).append(row)
            else:
                buckets[_category_bucket(config)].append(row)
        header = (
            "<tr><th>服务标识</th><th>管理群</th>"
            "<th>收受影响人</th><th>启用</th>"
            "<th>定时规则</th><th>依赖</th><th>描述</th><th>模板</th>"
            "<th>最近运行</th><th>操作</th></tr>"
        )
        group_header = (
            "<tr><th>服务标识</th>"
            "<th>收受影响人</th><th>启用</th>"
            "<th>定时规则</th><th>依赖</th><th>描述</th><th>模板</th>"
            "<th>最近运行</th><th>操作</th></tr>"
        )
        # 视角切换 tab（服务端渲染两链接；无 SPA，维持零依赖）。
        tabs = (
            "<div class=\"tabs\">"
            f"<a class=\"{'active' if view == 'group' else ''}\" "
            "href=\"/pipelines?view=group\">按管理群</a>"
            f"<a class=\"{'active' if view == 'class' else ''}\" "
            "href=\"/pipelines?view=class\">按功能类别</a>"
            "</div>"
        )
        confirm_mask = (
            "<div id=\"confirm-mask\" class=\"modal-mask\">"
            "<div class=\"modal-box\"><div id=\"confirm-text\"></div>"
            "<div class=\"modal-btns\">"
            "<button onclick=\"confirmOk()\">确定</button>"
            "<button onclick=\"closeConfirm()\">取消</button>"
            "</div></div></div>"
            # 收件人覆盖抽屉（方案 B，2026-10-12）：只改 @ 名单
            "<div id=\"recipient-mask\" class=\"modal-mask\">"
            "<div class=\"modal-box\" style=\"max-width:600px\">"
            "<div id=\"recipient-msg\" class=\"flash err\" "
            "style=\"display:none\"></div>"
            "<div id=\"recipient-body\"></div>"
            "<div class=\"modal-btns\">"
            "<button onclick=\"closeRecipientOverride()\">关闭</button>"
            "</div></div></div>"
        )
        # 改定时时间弹窗（2026-10-12）：把「定时规则」与「改时间」收在一处——
        # 顶部显示当前规则（中文+原表达式），下方选时刻并实时预览改后规则，
        # 避免「定时规则」列与时间编辑各说一半（用户 2026-10-12 反馈逻辑重叠）。
        schedule_mask = (
            "<div id=\"schedule-mask\" class=\"modal-mask\">"
            "<div class=\"modal-box\">"
            "修改 <b id=\"schedule-edit-service\"></b> 的定时时间"
            "<div>当前定时规则：<span id=\"schedule-edit-rule\"></span></div>"
            "<div class=\"hint\">原表达式：<span id=\"schedule-edit-cron\"></span>"
            "（只改时刻，工作日/每月几号等形态保留）</div>"
            "<div class=\"modal-btns\">"
            "<select id=\"schedule-edit-hour\" "
            "onchange=\"updateScheduleEditPreview()\">"
            + "".join(
                f"<option value=\"{h}\">{h:02d}</option>" for h in range(24)
            )
            + "</select><span class=\"hint\">时</span>"
            "<select id=\"schedule-edit-minute\" "
            "onchange=\"updateScheduleEditPreview()\">"
            "<option value=\"0\">00</option><option value=\"30\">30</option>"
            "</select><span class=\"hint\">分</span>"
            "<button onclick=\"submitScheduleEdit()\">确定</button>"
            "<button onclick=\"closeScheduleEdit()\">取消</button>"
            "</div>"
            "<div class=\"hint\" id=\"schedule-edit-preview\"></div>"
            "</div></div>"
        )
        table = ["<h1>定时任务（管道注册表）</h1>" + tabs]
        if view == "group":
            # 分组顺序：城市区域 → 跨区域线（渠道/线下整体）→ 未登记区域
            # → 平台/无群（恒置末位）；不得丢行。
            ordered = [r for r in _GROUP_ORDER if buckets.get(r)]
            ordered += sorted(
                r for r in buckets
                if r is not None and r not in _GROUP_ORDER and buckets[r])
            if buckets.get(None):
                ordered.append(None)
            for region in ordered:
                rows = buckets[region]
                table.append(
                    f"<h2>{_esc(_pipeline_group_title(region))}"
                    f"（{len(rows)} 项）</h2>"
                    f"<table>{group_header}" + "".join(rows) + "</table>"
                )
            table.append(
                "<p class=\"hint\">按管理群视角：同一区域的服务归为一组，"
                "一眼看清「这个群会收到哪些任务」；「渠道」「线下整体」为"
                "跨区域线，单独成组；无绑定区域的应用线/平台服务归入"
                "「平台 / 无群」。点「按功能类别」可按功能与定时规则查看。"
                "配置存 Nacos 注册表（PIPELINES 组），开关与新增在下一个"
                "调度轮询（≤30 秒）生效；「模板」= 调度器能否把该服务标识"
                "翻译成可执行命令；「收受影响人」= 催办覆盖面（日报机器人="
                "名册在册人员，名册未登记则该区域在册填报人全员兜底；"
                "渠道到齐=名册店铺负责人），非催办行无。</p>"
                + confirm_mask + schedule_mask
            )
        else:
            for title in _CATEGORY_TABLE_ORDER:
                table.append(
                    f"<h2>{title}</h2><table>{header}"
                    + "".join(buckets[title]) + "</table>"
                )
            table.append(
                "<p class=\"hint\">配置存 Nacos 注册表（PIPELINES 组），开关与新增在"
                "下一个调度轮询（≤30 秒）生效；「模板」= 调度器能否把该服务标识"
                "翻译成可执行命令；「管理群」= 该线服务的钉钉群（按区域映射）；"
                "分表=按功能（注册表 category 真源，未配置按服务标识推导兜底；"
                "2026-10-10 裁决按功能分表——群名是投递目标，管理群列已承载）；"
                "「收受影响人」= 催办覆盖面（日报机器人=名册在册人员，名册未登记"
                "则该区域在册填报人全员兜底；渠道到齐=名册店铺负责人），非催办行无。</p>"
                + confirm_mask + schedule_mask
            )
        table = "".join(table)
        add_form = (
            "<h2>新增定时任务</h2>"
            "<form class=\"inline\" id=\"pipeline-add-form\" "
            "onsubmit=\"addPipeline(event)\">"
            "<input name=\"service_id\" placeholder=\"服务标识（小写字母/数字/-/_）\" required>"
            "<select name=\"kind\">"
            "<option value=\"business\">业务线</option>"
            "<option value=\"apps\">应用线</option>"
            "</select>"
            "<select name=\"category\">"
            "<option value=\"\">功能（留空按标识推导）</option>"
            + "".join(
                f"<option value=\"{_esc(c)}\">{_esc(c)}</option>"
                for c in SUPPORTED_CATEGORIES
            )
            + "</select>"
            # 定时构建器（2026-10-10 裁决：预设+时点选择，不手写 cron；
            # 提交时由 JS 合成 cron，服务端 croniter 校验兜底；「自定义」
            # 仍开 cron 原文输入并实时中文预览——展示安全、改错即见）。
            "<select name=\"schedule_preset\" "
            "onchange=\"schedulePresetChange(this.form)\">"
            "<option value=\"daily\">每天</option>"
            "<option value=\"weekday\">工作日（周一至周五）</option>"
            "<option value=\"weekly\">每周</option>"
            "<option value=\"monthly\">每月</option>"
            "<option value=\"hourly\">每小时整点</option>"
            "<option value=\"custom\">自定义 cron</option>"
            "</select>"
            "<select name=\"schedule_hour\" "
            "onchange=\"updateSchedulePreview(this.form)\">"
            + "".join(
                f"<option value=\"{h}\"{' selected' if h == 18 else ''}>"
                f"{h:02d}</option>"
                for h in range(24)
            )
            + "</select><span class=\"hint\">时</span>"
            "<select name=\"schedule_minute\" "
            "onchange=\"updateSchedulePreview(this.form)\">"
            "<option value=\"0\">00</option>"
            "<option value=\"30\">30</option>"
            "</select><span class=\"hint\">分</span>"
            "<select name=\"schedule_dow\" style=\"display:none\" "
            "onchange=\"updateSchedulePreview(this.form)\">"
            "<option value=\"1\">周一</option><option value=\"2\">周二</option>"
            "<option value=\"3\">周三</option><option value=\"4\">周四</option>"
            "<option value=\"5\">周五</option><option value=\"6\">周六</option>"
            "<option value=\"0\">周日</option>"
            "</select>"
            "<input name=\"schedule_dom\" type=\"number\" min=\"1\" max=\"31\" "
            "value=\"1\" style=\"display:none;width:64px\" "
            "onchange=\"updateSchedulePreview(this.form)\">"
            "<input name=\"schedule\" placeholder=\"cron（分 时 日 月 周）\" "
            "style=\"display:none\" "
            "oninput=\"updateSchedulePreview(this.form)\">"
            "<span class=\"hint\" id=\"schedule-preview\"></span>"
            "<label><input type=\"checkbox\" name=\"enabled\" checked> 启用</label>"
            "<input name=\"depends_on\" placeholder=\"依赖的服务标识，逗号分隔（可空）\">"
            "<input name=\"description\" placeholder=\"描述\">"
            "<button type=\"submit\">新增</button></form>"
            "<p class=\"hint\">robot-&lt;区域&gt;（填报提醒）/ "
            "robot-check-&lt;区域&gt;（催办未填+DING）/ pages-&lt;区域&gt; / "
            "leaderboard-&lt;区域&gt; "
            "家族自动按后缀解析区域，注册即可调度；其他任意标识也可注册，"
            "但需先在调度器命令表加命令模板后才能触发（本页「模板」列可"
            "自查）。已存在的标识请用「操作」列开关，不可重复新增。</p>"
        )
        return HTMLResponse(_page("定时任务", table, add_form,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/pipelines/audit")
    def pipeline_audit_page(request: Request):
        viewer = require_admin(request)
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_pipeline_audit(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline audit load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        rows = []
        for row in entries:
            if isinstance(row, dict):
                values = (row.get("actor"), row.get("action"),
                          row.get("service_id"), row.get("detail"),
                          row.get("created_at"))
            else:
                values = row
            actor, action, service_id, detail, created_at = values
            rows.append(
                f"<tr><td>{_esc(_fmt_time(created_at))}</td><td>{_esc(actor)}</td>"
                f"<td>{_esc(_label(_ACTION_LABEL, action))}</td>"
                f"<td>{_esc(service_id)}</td>"
                f"<td>{_esc(detail)}</td></tr>"
            )
        table = (
            "<h1>定时任务审计流水</h1>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th>"
            "<th>服务标识</th><th>详情</th></tr>"
            + "".join(rows) + "</table>"
            "<p class=\"hint\">本表只记注册表变更（开关/新增）；每次实际"
            "运行（定时与「立即运行一次」见「执行流水」页）。"
            # G2（2026-10-12 断点报告）：审计分散四页的交叉索引，防误判漏审。
            "其他审计：<a href=\"/roster\">填报名册</a>页底=名册变更流水、"
            "<a href=\"/audit\">授权审计</a>=成员授权变更、"
            "<a href=\"/calendar\">工作日历</a>页底=日历改判流水。</p>"
        )
        return HTMLResponse(_page("任务审计", table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.get("/pipelines/history")
    def pipeline_history_page(request: Request, service_id: str = ""):
        viewer = require_admin(request)
        service_filter = service_id.strip()
        try:
            with db_connector() as connection:
                entries = ops_control.fetch_run_history(
                    connection, service_id=service_filter or None
                )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except Exception as exc:
            _LOGGER.warning("ops-web run history load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        history_keys = ("id", "service_id", "trigger_type", "status",
                        "exit_code", "note", "started_at", "finished_at")
        rows = []
        for row in entries:
            if isinstance(row, dict):
                get = row.get
            else:
                get = lambda key, r=row: r[history_keys.index(key)]
            exit_code = get("exit_code")
            rows.append(
                f"<tr><td>{_esc(_fmt_time(get('started_at')))}</td>"
                f"<td>{_service_name_cell(get('service_id'))}</td>"
                f"<td>{_esc(_label(_TRIGGER_LABEL, get('trigger_type')))}</td>"
                f"<td>{_esc(_label(_RUN_STATUS_LABEL, get('status')))}</td>"
                f"<td>{'—' if exit_code is None else _esc(exit_code)}</td>"
                f"<td>{_esc(_fmt_time(get('finished_at')))}</td>"
                f"<td>{_esc(get('note'))}</td></tr>"
            )
        table = (
            "<h1>定时任务执行流水</h1>"
            "<form class=\"inline\" method=\"get\" action=\"/pipelines/history\">"
            "<input name=\"service_id\" placeholder=\"按服务标识过滤（可空）\""
            f" value=\"{_esc(service_filter)}\">"
            "<button type=\"submit\">过滤</button></form>"
            "<table><tr><th>开始时间</th><th>服务标识</th><th>触发</th>"
            "<th>状态</th><th>退出码</th><th>结束时间</th><th>备注</th></tr>"
            + "".join(rows) + "</table>"
            "<p class=\"hint\">调度器每次实际点火（定时与「立即运行一次」同"
            "路径）落一行；锁被占用/运行异常记为失败且无退出码。依赖未满足"
            "的暂缓与非法 cron 不是运行，只走调度器日志。时间为北京时间。</p>"
        )
        return HTMLResponse(_page("执行流水", table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.post("/api/pipelines/toggle")
    async def pipeline_toggle(request: Request):
        viewer = require_admin(request)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        enabled = payload.get("enabled")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            config_publisher(replace(config, enabled=enabled))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid,
                    "enable" if enabled else "disable", service_id,
                    detail=config.schedule or "",
                )
        except Exception as exc:
            # 发布已生效而审计没落库：变更存在但流水缺失，必须显眼。
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/update")
    async def pipeline_schedule_update(request: Request):
        """修改既有线的定时时间（read-modify-write，只换 cron 的分/时两段）。

        与 toggle 同型：读注册表 → 改 → 整条回写 → 审计同生共死。只接收
        时(0-23)/分(00|30) 两维（运维不手写 cron），保留原 cron 的日/月/周，
        因此「工作日 18:00」「每月 1 日」这类形态不被抹平成每天。
        """
        viewer = require_admin(request)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        hour = payload.get("hour")
        minute = payload.get("minute")
        if (not ops_control.SERVICE_ID_RE.match(service_id or "")
                or not isinstance(hour, int) or isinstance(hour, bool)
                or not isinstance(minute, int) or isinstance(minute, bool)
                or not 0 <= hour <= 23 or minute not in (0, 30)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if service_id not in _fleet():
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = config_source.get_pipeline(service_id)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline read failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # 仅替换 cron 的第 0 段(分)/第 1 段(时)，保留 日/月/周；非 5 段
        # （空/常驻/异常）回落每天，避免拼出非法表达式。
        fields = (config.schedule or "").split()
        if len(fields) == 5:
            new_cron = f"{minute} {hour} {fields[2]} {fields[3]} {fields[4]}"
        else:
            new_cron = f"{minute} {hour} * * *"
        try:
            # 只借它做 croniter 校验；返回值会丢 sources/reads，不用于发布。
            ops_control.validate_pipeline_fields(
                service_id, config.kind, new_cron,
                enabled=config.enabled, description=config.description,
                depends_on=config.depends_on, category=config.category,
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(replace(config, schedule=new_cron))
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "schedule", service_id,
                    detail=new_cron,
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "schedule": new_cron})

    @app.post("/api/pipelines/add")
    async def pipeline_add(request: Request):
        viewer = require_admin(request)
        if config_publisher is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        payload = await _json_body(request)
        depends_on = payload.get("depends_on") or []
        if not isinstance(depends_on, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config = ops_control.validate_pipeline_fields(
                payload.get("service_id"),
                payload.get("kind"),
                payload.get("schedule"),
                enabled=payload.get("enabled", True),
                description=payload.get("description", ""),
                depends_on=depends_on,
                category=payload.get("category") or None,
            )
        except ops_control.OpsControlError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if config.service_id in _fleet():
            # 已存在的线走「操作」列开关；重复新增会静默覆盖 Nacos 条目。
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            config_publisher(config)
        except Exception as exc:
            _LOGGER.warning("ops-web pipeline publish failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        try:
            with db_connector() as connection:
                ops_control.insert_pipeline_audit(
                    connection, viewer.userid, "add", config.service_id,
                    detail=config.schedule or "",
                )
        except Exception as exc:
            _LOGGER.error(
                "ops-web pipeline audit write failed AFTER publish: %s",
                type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/run-once")
    async def pipeline_run_once(request: Request):
        viewer = require_admin(request)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not ops_control.SERVICE_ID_RE.match(service_id or ""):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not has_command(service_id):
            # 无命令模板的 id 触发不了（C 方案：允许注册但不允许空跑）。
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                if ops_control.has_pending_request(connection, service_id):
                    raise HTTPException(
                        status_code=400, detail=ErrorDetail.BAD_REQUEST
                    )
                request_id = ops_control.insert_run_request(
                    connection, service_id, viewer.userid,
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning("ops-web run-once write failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "request_id": request_id})

    # -- 填报名册（dim_report_roster，ops-web 唯一写方）---------------------

    @app.get("/roster")
    def roster_page(request: Request):
        viewer = require_roster_manager(request)
        try:
            with db_connector() as connection:
                entries = report_roster.fetch_roster(connection)
                audits = report_roster.fetch_roster_audit(connection, limit=20)
                qudao_targets = channel_target.fetch_store_targets(connection)
                qudao_numbers = report_roster.fetch_store_numbers(connection)
                # 区域个人月目标（dim_report_target，「所有数据入库」）：
                # 默认当月（北京时间），?month=YYYY-MM 可切到下月提前
                # 录入（与 target-rollover 结转管道配合：人工已录入的键
                # 结转时不覆盖）。
                current_month = _current_year_month()
                selected_month = (
                    request.query_params.get("month") or current_month
                )
                if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])",
                                    selected_month):
                    selected_month = current_month
                region_targets = {
                    scope: report_roster.fetch_person_targets(
                        connection, scope, selected_month)
                    for scope in report_roster.SCOPES if scope != "qudao"
                }
        except Exception as exc:
            _LOGGER.warning("ops-web roster load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

        by_scope = {scope: [] for scope in report_roster.SCOPES}
        for row in entries:
            by_scope.setdefault(row["scope"], []).append(row)

        sort = request.query_params.get("sort") or "channel"
        if sort not in _QUDAO_SORTS:
            sort = "channel"
        sections = ["<h1>填报名册（谁可以填哪些店/区域）</h1>"]
        # 目标月份导航（2026-10-09 月目标结转配套 + 运维反馈改日历选择器）：
        # 快捷链接（本月/下月）+ <input type="month"> 任意月直达——历史月
        # 核对、未来月提前录入都可；渠道门店目标不带月份键，不受此切换影响。
        next_month = _shift_year_month(current_month, 1)
        month_links = []
        for value, tag in ((current_month, "本月"), (next_month, "下月")):
            label = f"{value}（{tag}）"
            month_links.append(
                f"<strong>{_esc(label)}</strong>"
                if value == selected_month
                else f"<a href=\"/roster?month={_esc(value)}\">{_esc(label)}</a>"
            )
        sections.append(
            "<p>区域个人月目标的目标月份：" + " ｜ ".join(month_links)
            + "　<form method=\"get\" action=\"/roster\" "
              "style=\"display:inline\">"
            f"<input type=\"month\" name=\"month\" "
            f"value=\"{_esc(selected_month)}\" required>"
            "<button type=\"submit\">切换</button></form>"
            + "<span class=\"hint\">（每月 1 日 06:00 上月目标自动结转到当月，"
              "人工已录入的不覆盖；渠道门店目标不带月份，常年有效）</span></p>"
        )
        for scope in report_roster.SCOPES:
            rows = by_scope.get(scope) or []
            if scope == "qudao":
                sections.append(
                    _qudao_section(rows, qudao_targets, qudao_numbers, sort=sort)
                )
                continue
            scope_label = _label(_SCOPE_LABEL, scope)
            sections.append(f"<h2>{_esc(scope_label)}"
                            f"<span class=\"hint\">（{len(rows)} 条）</span></h2>")
            if not rows:
                sections.append("<p class=\"hint\">暂无记录"
                                + ("——该范围当前不拦截（没登记就不限制填报，维持现状）。"
                                   if scope != "qudao" else
                                   "——渠道机器人自动回退读门店月目标表。")
                                + "</p>")
                continue
            # 统一链接行模板（2026-10-07 运维裁决，与渠道门店同构）：
            # 渠道/地区 | 店铺/对象 | 负责人 | 代填报人 | 月目标（元）|
            # 状态 | 备注 | 更新 | 操作。地区列整段合并、对象列按值合并；
            # 月目标取区域 Nacos monthlyTargets（个人粒度，无则 —）。
            person_targets = region_targets.get(scope) or {}
            sorted_rows = sorted(
                rows,
                key=lambda r: (str(r["entity_key"]),
                               1 if r.get("role") == "deputy" else 0,
                               str(r["person_name"]), int(r["id"])),
            )
            spans: dict = {}
            for row in sorted_rows:
                key = str(row["entity_key"])
                spans[key] = spans.get(key, 0) + 1
            seen: set = set()
            body = []
            for position, row in enumerate(sorted_rows):
                cells = []
                if position == 0:
                    cells.append(
                        f"<td rowspan=\"{len(sorted_rows)}\">"
                        f"{_esc(scope_label)}</td>"
                        if len(sorted_rows) > 1
                        else f"<td>{_esc(scope_label)}</td>"
                    )
                key = str(row["entity_key"])
                if key not in seen:
                    seen.add(key)
                    cells.append(
                        f"<td rowspan=\"{spans[key]}\">{_esc(key)}</td>"
                        if spans[key] > 1 else f"<td>{_esc(key)}</td>"
                    )
                personal_target = person_targets.get(row["person_name"])
                target_value = (
                    f"{float(personal_target):.0f}"
                    if personal_target is not None else ""
                )
                updated = _fmt_time(row.get("updated_at")) or "—"
                name_html = _link_name_html(row)
                # 按 role 分列（2026-10-08 修复：人员段此前忽略 role，
                # 代填人全显示在负责人列——与 qudao 段同构）。
                owner_cell, deputy_cell = (
                    ("<td></td>", f"<td>{name_html}</td>")
                    if row.get("role") == "deputy"
                    else (f"<td>{name_html}</td>", "<td></td>")
                )
                cells.append(
                    f"<td><input class=\"person-target\" "
                    f"data-scope=\"{_esc(scope)}\" "
                    f"data-name=\"{_esc(row['person_name'])}\" "
                    f"value=\"{_esc(target_value)}\" "
                    f"placeholder=\"目标（元）\"></td>"
                    f"{owner_cell}{deputy_cell}"
                    f"<td>{_status_select_html(row)}</td>"
                    f"<td>{_esc(row.get('note'))}</td>"
                    f"<td><span class=\"hint\">{_esc(updated)}</span></td>"
                    f"<td>{_delete_btn_html(row)}</td>"
                )
                body.append("<tr>" + "".join(cells) + "</tr>")
            sections.append(
                "<table><tr><th>渠道/地区</th><th>店铺/对象</th>"
                "<th>月目标（元）</th><th>负责人</th><th>代填报人</th>"
                "<th>状态</th><th>备注</th><th>更新</th><th>操作</th></tr>"
                + "".join(body) + "</table>"
                f"<p><button onclick=\"saveRegionTargets('{_esc(scope)}', "
                f"'{_esc(selected_month)}')\">"
                f"保存{_esc(scope_label)}{_esc(selected_month)}月目标</button>"
                "<span class=\"hint\">（单位：元，空白 = 清除该人当月目标；"
                "保存即生效——机器人报数快照同步读取，逐条落审计）</span></p>"
            )

        scope_options = "".join(
            f"<option value=\"{_esc(scope)}\">{_esc(_label(_SCOPE_LABEL, scope))}</option>"
            for scope in report_roster.SCOPES
        )
        entity_options = "".join(
            f"<option value=\"{_esc(entity)}\">{_esc(_label(_ENTITY_LABEL, entity))}</option>"
            for entity in report_roster.ENTITY_TYPES
        )
        add_form = (
            "<h2>新增名册记录</h2>"
            "<form class=\"inline\" onsubmit=\"addRoster(event)\">"
            f"<select name=\"scope\">{scope_options}</select>"
            f"<select name=\"entity_type\">{entity_options}</select>"
            "<input name=\"entity_key\" placeholder=\"对象（店名/部门名/区域）\" required>"
            "<input name=\"person_name\" placeholder=\"填报人姓名\" required>"
            "<select name=\"role\">"
            "<option value=\"owner\">负责人</option>"
            "<option value=\"deputy\">代填报人</option>"
            "</select>"
            "<input name=\"aliases\" placeholder=\"别名，逗号分隔（可空）\">"
            "<input name=\"note\" placeholder=\"备注（可空）\">"
            "<button type=\"submit\">新增</button></form>"
            "<p class=\"hint\">渠道门店（qudao）：管理「谁可以填哪些店」，渠道机器人"
            "实时生效；日报区域（杭州/绍兴等）：该范围一旦有人名记录即启用白名单"
            "（只准在册人员填报），无记录则不拦截；餐饮/部门先登记，机器人后续接入。"
            "「删除」会留审计，日常调整建议用「停用」。"
            "<strong>人名一律用通讯录本名（不用花名/昵称）</strong>；"
            "别名仅用于群昵称与本名不一致的匹配容错。"
            "角色：负责人占业绩归属，代填报人仅可填报不占业绩；"
            "改角色 = 用新角色重新「新增」同人同对象（覆盖生效）。"
            "本页新增/删除/状态修改需「名册管理」授权"
            "（成员与授权页单人授权：scope = roster）。</p>"
        )
        audit_rows = "".join(
            f"<tr><td>{_esc(_fmt_time(row['created_at']))}</td>"
            f"<td>{_esc(row['actor'])}</td>"
            f"<td>{_esc(_label(_ACTION_LABEL, row['action']))}</td>"
            f"<td>{_esc(_label(_SCOPE_LABEL, row['scope']))}</td>"
            f"<td>{_esc(row['entity_key'])}</td>"
            f"<td>{_esc(row['person_name'])}</td>"
            f"<td>{_esc(row.get('detail'))}</td></tr>"
            for row in audits
        )
        audit_table = (
            "<h2>名册变更流水（最近 20 条）</h2>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th><th>范围</th>"
            "<th>对象</th><th>填报人</th><th>详情</th></tr>"
            + (audit_rows or "<tr><td colspan=\"7\" class=\"hint\">暂无变更</td></tr>")
            + "</table>"
        )
        return HTMLResponse(_page("填报名册", "".join(sections), add_form,
                                  audit_table,
                                  viewer_name=viewer.name or viewer.userid))

    @app.post("/api/roster/add")
    async def roster_add(request: Request):
        viewer = require_roster_manager(request)
        payload = await _json_body(request)
        aliases = payload.get("aliases") or []
        if not isinstance(aliases, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            entry = report_roster.validate_roster_fields(
                payload.get("scope"),
                payload.get("entity_type"),
                payload.get("entity_key"),
                payload.get("person_name"),
                aliases=aliases,
                note=payload.get("note", ""),
                role=payload.get("role") or "owner",
            )
        except report_roster.ReportRosterError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                report_roster.upsert_roster_entry(
                    connection, entry, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster add failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/toggle")
    async def roster_toggle(request: Request):
        viewer = require_roster_manager(request)
        payload = await _json_body(request)
        roster_id = payload.get("id")
        enabled = payload.get("enabled")
        if (isinstance(roster_id, bool) or not isinstance(roster_id, int)
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = report_roster.set_roster_enabled(
                    connection, roster_id, enabled, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/delete")
    async def roster_delete(request: Request):
        viewer = require_roster_manager(request)
        payload = await _json_body(request)
        roster_id = payload.get("id")
        if isinstance(roster_id, bool) or not isinstance(roster_id, int):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = report_roster.delete_roster_entry(
                    connection, roster_id, actor=viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web roster delete failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/roster/region-targets")
    async def roster_region_targets(request: Request):
        """保存某区域当月个人月目标（dim_report_target，「所有数据入库」）：
        逐条校验+upsert+审计；空白（null）= 清除该人当月目标。"""
        viewer = require_roster_manager(request)
        payload = await _json_body(request)
        scope = payload.get("scope")
        rows = payload.get("rows")
        if scope not in report_roster.SCOPES or scope == "qudao" \
                or not isinstance(rows, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 目标月份（2026-10-09 起可指定下月提前录入；缺省当月，非法回退当月）
        year_month = payload.get("year_month") or _current_year_month()
        if not isinstance(year_month, str) or not re.fullmatch(
                r"20\d{2}-(0[1-9]|1[0-2])", year_month):
            year_month = _current_year_month()
        try:
            entries = [
                report_roster.validate_target_fields(
                    scope,
                    (row or {}).get("person_name") if isinstance(row, dict)
                    else None,
                    year_month,
                    (row or {}).get("monthly_target")
                    if isinstance(row, dict) else None,
                )
                for row in rows
            ]
        except report_roster.ReportRosterError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                for entry in entries:
                    report_roster.upsert_report_target(
                        connection, entry, actor=viewer.userid
                    )
        except Exception as exc:
            _LOGGER.warning(
                "ops-web region targets save failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "saved": len(entries)})

    @app.post("/api/roster/publish-snapshot")
    async def roster_publish_snapshot(request: Request):
        """发布月度目标快照（S2）：目标覆盖 raw + 名册投影 owners + 触发提取。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        rows = payload.get("rows")
        if not isinstance(rows, list):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        # 渠道缺省时从名册编号表反查（页面只对有把握的店带 channel）。
        try:
            with db_connector() as connection:
                numbers = report_roster.fetch_store_numbers(connection)
        except Exception as exc:
            _LOGGER.warning("ops-web publish preflight failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and not (row.get("channel") or "").strip():
                store = row.get("store_name")
                row["channel"] = numbers.get(store, ("", None))[0]
        try:
            target_rows = channel_target.validate_target_rows(rows)
        except channel_target.ChannelTargetError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as mart_connection:
                raw_connection = connect(settings.dingtalk_database)
                try:
                    deleted, inserted, missing = channel_target.publish_snapshot(
                        raw_connection, target_rows, actor=viewer.userid,
                        roster_connection=mart_connection,
                    )
                finally:
                    raw_connection.close()
                request_id = ops_control.insert_run_request(
                    mart_connection, "extract-mart", viewer.userid
                )
        except Exception as exc:
            _LOGGER.warning("ops-web publish snapshot failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({
            "status": "ok", "deleted": deleted, "inserted": inserted,
            "missing_owners": missing, "extract_request_id": request_id,
        })

    # -- 工作日历（dim_calendar_override DB 裁决层，2026-10-10「给非技术
    # 同学用」：一键导入法定假日 + 点日期改上班/休息，不碰 git/CLI）------

    #: 日历源徽标（单元格角标；local=种子/规则基线不标）。
    _CAL_SOURCE_BADGE = {
        "holiday_cn": '<span class="cal-src cal-src-holiday_cn">法</span>',
        "manual": '<span class="cal-src cal-src-manual">裁</span>',
        "rule_fallback": '<span class="cal-src cal-src-rule_fallback">兜</span>',
    }

    def _calendar_seed_path():
        seed_path = getattr(settings, "calendar_seed_path", None)
        if seed_path is None:
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return seed_path

    @app.get("/calendar")
    def calendar_page(request: Request, month: str = ""):
        viewer = require_admin(request)
        month = (month or "").strip()
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            month = _current_year_month()
        year, mon = (int(part) for part in month.split("-"))
        try:
            with db_connector() as connection:
                days = calendar_store.fetch_month_days(connection, year, mon)
                audits = calendar_store.fetch_recent_audits(connection, limit=20)
        except Exception as exc:
            _LOGGER.warning("ops-web calendar load failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)

        cells = {day.day: (day, is_workday, source, note)
                 for day, is_workday, source, note in days}
        today = datetime.now(_BJT).date()
        first_weekday = date(year, mon, 1).weekday()  # 周一=0
        row = ['<td class="cal-empty"></td>'] * first_weekday
        grid_rows = []
        import calendar as _pycal
        for day_no in range(1, _pycal.monthrange(year, mon)[1] + 1):
            info = cells.get(day_no)
            if info is None:
                row.append(f'<td class="cal-empty">{day_no}</td>')
            else:
                day, is_workday, source, note = info
                cls = "cal-day " + ("cal-rest" if not is_workday else "cal-work")
                if day == today:
                    cls += " cal-today"
                badge = _CAL_SOURCE_BADGE.get(source, "")
                title = html.escape(note or "", quote=True)
                row.append(
                    f'<td class="{cls}" title="{title}" '
                    f"onclick=\"calendarToggle('{day.isoformat()}', "
                    f"{'true' if is_workday else 'false'})\">"
                    f"{day_no}{badge}</td>"
                )
            if len(row) == 7:
                grid_rows.append("<tr>" + "".join(row) + "</tr>")
                row = []
        if row:
            row += ['<td class="cal-empty"></td>'] * (7 - len(row))
            grid_rows.append("<tr>" + "".join(row) + "</tr>")

        prev_month = _shift_year_month(month, -1)
        next_month = _shift_year_month(month, 1)
        nav = (
            '<div class="cal-nav">'
            f'<a href="/calendar?month={prev_month}">← 上一月</a>'
            f"<strong>{year} 年 {mon} 月</strong>"
            f'<a href="/calendar?month={next_month}">下一月 →</a>'
            f'<a href="/calendar">本月</a>'
            '<input type="month" '
            f'value="{month}" '
            'onchange="if(this.value)location.href=\'/calendar?month=\'+this.value">'
            "</div>"
        )
        legend = (
            '<p class="hint">蓝底=休息 · 白底=上班 · 徽标：法=法定节假日导入 / '
            "裁=人工裁决 / 兜=规则兜底（种子未覆盖月，法定假未知按工作日计）"
            " · 点任意日期可切换 上班/休息（立即重写当月日历，全部操作留审计）"
            "</p>"
        )
        grid = (
            '<table class="cal-table"><tr><th>一</th><th>二</th><th>三</th>'
            "<th>四</th><th>五</th><th>六</th><th>日</th></tr>"
            + "".join(grid_rows) + "</table>"
        )
        import_form = (
            "<h2>导入法定节假日（holiday-cn 官方转录）</h2>"
            '<form class="inline" onsubmit="calendarImportYear(event)">'
            f'<input name="year" type="number" min="2020" max="2100" '
            f'value="{year + 1 if mon >= 10 else year}" required>'
            "<button type=\"submit\">一键导入该年法定假日</button>"
            '<span class="hint">该年法定导入行整年替换，人工裁决（裁）不动；'
            "随后按当前口径重写全年日历。国务院一般 10-11 月发布次年安排。"
            "</span></form>"
            "<h2>恢复规则</h2>"
            '<form class="inline" onsubmit="calendarResetMonth('
            f"'{month}');return false"
            '">'
            f"<button type=\"submit\">清除 {month} 人工裁决</button>"
            '<span class="hint">只清「裁」行（法定导入「法」保留），'
            "当月回到规则基线口径。</span></form>"
        )
        audit_rows = []
        for entry in audits:
            get = entry.get if isinstance(entry, dict) else None
            if get is not None:
                actor = get("actor")
                action = get("action")
                biz_date = get("business_date")
                yr = get("year")
                mon2 = get("month")
                detail = get("detail")
                created = get("created_at")
            else:
                actor, action, biz_date, yr, mon2, detail, created = entry
            when = biz_date.isoformat() if isinstance(biz_date, date) else (
                f"{yr}-{int(mon2):02d}" if yr and mon2 else "—"
            )
            audit_rows.append(
                f"<tr><td>{_esc(_fmt_time(created))}</td><td>{_esc(actor)}</td>"
                f"<td>{_esc(_label(_ACTION_LABEL, action))}</td>"
                f"<td>{_esc(when)}</td><td>{_esc(detail)}</td></tr>"
            )
        audit_table = (
            "<h2>最近修正记录</h2>"
            "<table><tr><th>时间</th><th>操作人</th><th>动作</th>"
            "<th>日期</th><th>详情</th></tr>"
            + "".join(audit_rows) + "</table>"
        )
        return HTMLResponse(_page(
            "工作日历",
            f"<h1>工作日历（{year} 年 {mon} 月）</h1>",
            nav, legend, grid, import_form, audit_table,
            viewer_name=viewer.name or viewer.userid,
        ))

    @app.post("/api/calendar/toggle")
    async def calendar_toggle(request: Request):
        """点日期改 上班/休息：写 manual 覆盖 + 即时重写当月 + 审计。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        note = payload.get("note") or ""
        is_workday = payload.get("is_workday")
        try:
            day = date.fromisoformat(str(payload.get("date")))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        if not isinstance(is_workday, bool) \
                or not isinstance(note, str) or len(note) > 255:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                calendar_store.upsert_override(
                    connection, day, is_workday,
                    actor=viewer.userid, note=note,
                )
                calendar_store.rewrite_month(
                    connection, _calendar_seed_path(), day.year, day.month,
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar toggle failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok"})

    @app.post("/api/calendar/import-year")
    async def calendar_import_year(request: Request):
        """一键导入法定节假日（该年 holiday_cn 行整年替换 + 全年重写）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        year = payload.get("year")
        if isinstance(year, bool) or not isinstance(year, int) \
                or not 2020 <= year <= 2100:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                stats = calendar_store.import_year_overrides(
                    connection, year, actor=viewer.userid
                )
                rewritten = 0
                for month_no in range(1, 13):
                    rewritten += calendar_store.rewrite_month(
                        connection, _calendar_seed_path(), year, month_no,
                        strict=False,
                    )
        except CalendarImportError as exc:
            # holiday-cn 未发布/网络失败：原因透传给前端（不含敏感信息）
            return JSONResponse(
                {"status": "failed", "reason": str(exc)}, status_code=502
            )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar import failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "rewritten": rewritten, **stats})

    @app.post("/api/calendar/reset-month")
    async def calendar_reset_month(request: Request):
        """本月恢复规则：清除 manual 覆盖（法定导入保留）+ 即时重写。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        month = str(payload.get("month") or "")
        if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        year, mon = (int(part) for part in month.split("-"))
        try:
            with db_connector() as connection:
                deleted = calendar_store.reset_month_overrides(
                    connection, year, mon, actor=viewer.userid
                )
                calendar_store.rewrite_month(
                    connection, _calendar_seed_path(), year, mon
                )
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web calendar reset failed: %s", type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    # ------------------------------------------------------------------
    # 任务级收件人覆盖（方案 B，2026-10-12）：只改 @ 名单，不改消息正文
    # ------------------------------------------------------------------
    _service_id_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

    @app.get("/api/pipelines/recipient-override")
    async def recipient_override_get(request: Request):
        """某条服务线的全部覆盖行（含停用），供抽屉列表展示。"""
        require_admin(request)
        service_id = (request.query_params.get("service_id") or "").strip()
        if not _service_id_re.match(service_id):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                rows = recipient_override.fetch_service_overrides(
                    connection, service_id)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override read failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        # updated_at 是 DB DATETIME（pymysql 返回 datetime 对象），直接进
        # JSONResponse 会 json.dumps 失败 → 500（2026-10-12 线上实测：抽屉报
        # 「读取异常，请稍后重试」）。出口统一用 _fmt_time 归一为北京时间字符串，
        # 与页面其它时间展示口径一致。
        overrides = [
            dict(row, updated_at=_fmt_time(row.get("updated_at")))
            for row in rows
        ]
        return JSONResponse(
            {"status": "ok", "service_id": service_id, "overrides": overrides})

    @app.post("/api/pipelines/recipient-override/add")
    async def recipient_override_add(request: Request):
        """新增/重启用一条覆盖：先解析姓名快照，再落库（同事务审计）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        mode = payload.get("mode")
        target_type = payload.get("target_type")
        target_value = payload.get("target_value")
        note = payload.get("note") or ""
        if (not isinstance(service_id, str)
                or not _service_id_re.match(service_id.strip())
                or mode not in recipient_override.MODES
                or target_type not in recipient_override.TARGET_TYPES
                or not isinstance(target_value, str) or not target_value.strip()
                or not isinstance(note, str)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        unresolved = []
        resolved = None
        try:
            with db_connector() as connection:
                resolved_rows, unresolved = recipient_override.resolve_targets(
                    connection,
                    [{"target_type": target_type,
                      "target_value": target_value}],
                )
                resolved = resolved_rows[0].get("resolved_user_id")
                recipient_override.upsert_override(
                    connection, service_id.strip(), mode, target_type,
                    target_value, actor=viewer.userid, note=note,
                    resolved_user_id=resolved,
                )
        except recipient_override.RecipientOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override add failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse(
            {"status": "ok", "resolved_user_id": resolved,
             "unresolved": unresolved})

    @app.post("/api/pipelines/recipient-override/toggle")
    async def recipient_override_toggle(request: Request):
        """停用/启用一条覆盖（按主键 id）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        override_id = payload.get("id")
        enabled = payload.get("enabled")
        if (isinstance(override_id, bool) or not isinstance(override_id, int)
                or not isinstance(enabled, bool)):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = recipient_override.set_override_enabled(
                    connection, override_id, enabled, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override toggle failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/recipient-override/delete")
    async def recipient_override_delete(request: Request):
        """删除一条覆盖（审计留存被删行内容）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        override_id = payload.get("id")
        if isinstance(override_id, bool) or not isinstance(override_id, int):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                hit = recipient_override.delete_override(
                    connection, override_id, actor=viewer.userid)
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override delete failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        if not hit:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        return JSONResponse({"status": "ok"})

    @app.post("/api/pipelines/recipient-override/reset")
    async def recipient_override_reset(request: Request):
        """回退总开关：清空某条服务线全部覆盖（回名册口径）。"""
        viewer = require_admin(request)
        payload = await _json_body(request)
        service_id = payload.get("service_id")
        if not isinstance(service_id, str) \
                or not _service_id_re.match(service_id.strip()):
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        try:
            with db_connector() as connection:
                deleted = recipient_override.reset_service_overrides(
                    connection, service_id.strip(), actor=viewer.userid)
        except recipient_override.RecipientOverrideError:
            raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "ops-web recipient override reset failed: %s",
                type(exc).__name__)
            raise HTTPException(status_code=503, detail=ErrorDetail.UNAVAILABLE)
        return JSONResponse({"status": "ok", "deleted": deleted})

    return app


async def _json_body(request: Request):
    try:
        payload = await request.json()
    except Exception:
        payload = None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=ErrorDetail.BAD_REQUEST)
    return payload


def serve(application):
    import uvicorn
    uvicorn.run(application, host="0.0.0.0", port=8080)


def main():
    """从环境装配并启动；启动失败只打印一行安全消息（同 bi-web 纪律）。"""
    try:
        from common.public_data.pipeline_config import (
            build_config_publisher,
            build_config_source,
        )
        from common.public_data.scheduler import build_fleet_source

        settings = load_settings()
        session_secret = os.environ.get("BI_WEB_SESSION_SECRET") or None
        app_key = (os.environ.get("BI_DINGTALK_APPKEY") or "").strip()
        app_secret = (os.environ.get("BI_DINGTALK_APPSECRET") or "").strip()
        auth_client = (
            auth.DingTalkAuthClient(app_key, app_secret)
            if session_secret and app_key and app_secret
            else None
        )
        session_secure = os.environ.get(
            "BI_WEB_SESSION_SECURE", ""
        ).strip().lower() in ("1", "true", "yes")
        corp_id = (os.environ.get("BI_DINGTALK_CORPID") or "").strip() or None
        agent_id = (os.environ.get("BI_DINGTALK_AGENTID") or "").strip() or None
        config_source = build_config_source()
        try:
            fleet_source = build_fleet_source()
        except Exception:
            # 舰队清单地基（seed）缺失不拖垮权限管理面：定时任务页 503。
            _LOGGER.warning("ops-web fleet source unavailable at startup")
            fleet_source = None
        application = create_app(
            settings=settings,
            session_secret=session_secret,
            auth_client=auth_client,
            session_secure=session_secure,
            fleet_source=fleet_source,
            config_source=config_source,
            config_publisher=build_config_publisher(),
            corp_id=corp_id,
            agent_id=agent_id,
        )
    except Exception as exc:
        print(f"ops-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
