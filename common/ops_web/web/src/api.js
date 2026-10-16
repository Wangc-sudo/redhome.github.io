/**
 * ops-web「组织编排」前端 API 封装。
 *
 * 基址同源（/api/org/*），全部需要管理员登录态：
 * - 统一带 cookie（credentials: 'same-origin'）与 JSON Content-Type；
 * - 非 2xx 一律抛错，把后端 {detail} 转成可读中文消息；
 * - 401/403 归一提示「登录已过期或无权限，请重新登录」。
 */

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/** 把后端 detail 字段（字符串 / 对象）转成可读文本。 */
function readableDetail(detail, status) {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (detail != null) {
    try {
      return JSON.stringify(detail);
    } catch (err) {
      /* 忽略序列化异常，走兜底文案 */
    }
  }
  return `请求失败（HTTP ${status}）`;
}

/** 统一请求：返回解析后的 JSON（无 body 时为 null）。 */
async function request(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, {
      credentials: 'same-origin',
      headers: { ...JSON_HEADERS, ...(options.headers || {}) },
      ...options,
    });
  } catch (err) {
    throw new Error('网络异常，请检查连接后重试');
  }

  const text = await resp.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      data = null;
    }
  }

  if (!resp.ok) {
    const err = new Error(
      resp.status === 401 || resp.status === 403
        ? '登录已过期或无权限，请重新登录'
        : readableDetail(data && data.detail, resp.status),
    );
    // 携带 HTTP 状态码，供 router 守卫区分 401/403（跳登录）与其它错误。
    err.status = resp.status;
    throw err;
  }
  return data;
}

/** 拼查询串：跳过 undefined / null / 空串；值统一 URL 编码。 */
function queryString(params = {}) {
  const parts = [];
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue;
    parts.push(`${encodeURIComponent(key)}=${encodeURIComponent(value)}`);
  }
  return parts.length ? `?${parts.join('&')}` : '';
}

/** POST JSON 快捷封装。 */
function post(path, payload) {
  return request(path, {
    method: 'POST',
    body: JSON.stringify(payload || {}),
  });
}

/** 组织树总览：regions / pool / overrides。 */
export function getOverview() {
  return request('/api/org/overview', { method: 'GET' });
}

/**
 * 新增/更新一条归属覆盖。
 * @param {{user_id:string, region:(string|null), dept_id?:(string|null),
 *          dept_name?:(string|null), name?:(string|null), note?:(string|null)}} payload
 * region 为 null 表示「移出全部区域」（进入未归类池）。
 */
export function assignMember(payload) {
  return request('/api/org/assign', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * 归属搬迁（原子复合写）：改权威归属 + 同步双侧 person 名册 + 审计。
 * @param {{user_id:string, to_region:(string|null), person_name:string,
 *          dept_name?:(string|null), from_region?:(string|null),
 *          role?:string, note?:(string|null)}} payload
 * to_region 为 null 表示移入未归类池；后端单事务完成，避免半移动态。
 */
export function moveMember(payload) {
  return request('/api/org/move', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/** 停用/启用某人的覆盖（停用 = 回种子口径）。 */
export function toggleOverride(userId, enabled) {
  return request('/api/org/toggle', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId, enabled }),
  });
}

/** 删除某人的覆盖（回种子口径）。 */
export function deleteOverride(userId) {
  return request('/api/org/delete', {
    method: 'POST',
    body: JSON.stringify({ user_id: userId }),
  });
}

/** 清除全部覆盖（全部归还种子口径）。 */
export function resetOverrides() {
  return request('/api/org/reset', {
    method: 'POST',
    body: JSON.stringify({}),
  });
}

/**
 * 登记一个区域（消费侧组织目录；即使无成员也进树）。
 * @param {{region_key:string, label:string, sort_order?:number}} payload
 */
export function addRegion(payload) {
  return request('/api/org/region/add', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * 登记一个区域下的部门（消费侧组织目录；即使无成员也进树）。
 * @param {{region_key:string, dept_name:string, dept_id?:(string|null),
 *          sort_order?:number}} payload
 */
export function addDept(payload) {
  return request('/api/org/dept/add', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * 停用 / 启用一个登记区域。
 * @param {{region_key:string, enabled:boolean}} payload
 */
export function disableRegion(payload) {
  return request('/api/org/region/disable', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * 物理删除一个登记区域（级联删除其下部门）。
 * @param {{region_key:string}} payload
 */
export function deleteRegion(payload) {
  return request('/api/org/region/delete', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * 停用 / 启用一个登记部门。
 * @param {{region_key:string, dept_name:string, enabled:boolean}} payload
 */
export function disableDept(payload) {
  return request('/api/org/dept/disable', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * 物理删除一个登记部门。
 * @param {{region_key:string, dept_name:string}} payload
 */
export function deleteDept(payload) {
  return request('/api/org/dept/delete', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

// ---------------------------------------------------------------------------
// 全 SPA 迁移（C 阶段）：全域读写端点。函数名即后续页面调用契约，勿改名。
// POST 字段名以 app.py 各 handler 的 payload.get(...) 为准。
// ---------------------------------------------------------------------------

// -- 读 ---------------------------------------------------------------------

/** 当前会话能力×范围视图（SPA 启动/守卫用）。 */
export function getMe() {
  return request('/api/me', { method: 'GET' });
}

/** 区域/部门目录真源。 */
export function getRegions() {
  return request('/api/regions', { method: 'GET' });
}

/** 六源聚合审计读面。params: {source, actor, action, from, to}。 */
export function getRecords(params = {}) {
  const qs = queryString({
    source: params.source,
    actor: params.actor,
    action: params.action,
    from: params.from,
    to: params.to,
  });
  return request(`/api/records${qs}`, { method: 'GET' });
}

/** 名册 + 区域个人月目标读面。{month} 缺省当月。 */
export function getRoster({ month } = {}) {
  return request(`/api/roster${queryString({ month })}`, { method: 'GET' });
}

/** 某区域当月个人月目标读面。{scope, month}。 */
export function getRosterTargets({ scope, month } = {}) {
  return request(
    `/api/roster/targets${queryString({ scope, month })}`,
    { method: 'GET' },
  );
}

/** 渠道门店一体读面（qudao 段）。 */
export function getRosterQudao() {
  return request('/api/roster/qudao', { method: 'GET' });
}

/** 定时任务读面（分组/行装配）。 */
export function getPipelines() {
  return request('/api/pipelines', { method: 'GET' });
}

/** 定时任务审计读面。 */
export function getPipelinesAudit() {
  return request('/api/pipelines/audit', { method: 'GET' });
}

/** 定时任务执行流水读面（可按 serviceId 过滤）。 */
export function getPipelinesHistory(serviceId) {
  return request(
    `/api/pipelines/history${queryString({ service_id: serviceId })}`,
    { method: 'GET' },
  );
}

/** 某条服务线的收件人覆盖行 + 基名单 + 归因。 */
export function getRecipientOverride(serviceId) {
  return request(
    `/api/pipelines/recipient-override${queryString({ service_id: serviceId })}`,
    { method: 'GET' },
  );
}

/** 播报日历读面（整月天 + 最近修正）。month = 'YYYY-MM'。 */
export function getCalendar(month) {
  return request(`/api/calendar${queryString({ month })}`, { method: 'GET' });
}

/** 收件人总览读面（rules_html + 各 scope 分节 html）。 */
export function getRecipients() {
  return request('/api/recipients', { method: 'GET' });
}

/** 授权现状只读读面。 */
export function getGrants() {
  return request('/api/grants', { method: 'GET' });
}

/** 授权审计只读读面。 */
export function getAudit() {
  return request('/api/audit', { method: 'GET' });
}

// -- 写 ---------------------------------------------------------------------

/** 新增/更新一条名册项。payload: {scope, entity_type, entity_key, person_name,
 *  aliases?, note?, role?}。 */
export function addRosterEntry(payload) {
  return post('/api/roster/add', payload);
}

/** 停用/启用一条名册项。payload: {id, enabled}。 */
export function toggleRosterEntry(payload) {
  return post('/api/roster/toggle', payload);
}

/** 删除一条名册项。payload: {id}。 */
export function deleteRosterEntry(payload) {
  return post('/api/roster/delete', payload);
}

/** 保存某区域当月个人月目标。payload: {scope, rows:[{person_name,
 *  monthly_target}], year_month?}。 */
export function saveRegionTargets(payload) {
  return post('/api/roster/region-targets', payload);
}

/** 发布月度目标快照。payload: {rows:[...]}。 */
export function publishSnapshot(payload) {
  return post('/api/roster/publish-snapshot', payload);
}

/** 启用/停用一条定时任务。payload: {service_id, enabled}。 */
export function togglePipeline(payload) {
  return post('/api/pipelines/toggle', payload);
}

/** 改播报时间。payload: {service_id, times:[{hour, minute}]}。 */
export function updatePipelineTimes(payload) {
  return post('/api/pipelines/update', payload);
}

/**
 * 改线级参数（巡检告警线的阈值/冷却）。payload: {service_id, params}，
 * params 传 {} 即清空。只覆盖 params，其余运行态原样保留。
 */
export function updatePipelineParams(payload) {
  return post('/api/pipelines/params', payload);
}

/** 新增一条定时任务。payload: {service_id, kind, schedule, enabled?,
 *  description?, depends_on?, category?, params?}。 */
export function addPipeline(payload) {
  return post('/api/pipelines/add', payload);
}

/** 立即运行一次。payload: {service_id}。 */
export function runPipelineOnce(payload) {
  return post('/api/pipelines/run-once', payload);
}

/** 新增/重启用一条收件人覆盖。payload: {service_id, mode, target_type,
 *  target_value, note?}。 */
export function addRecipientOverride(payload) {
  return post('/api/pipelines/recipient-override/add', payload);
}

/** 停用/启用一条收件人覆盖。payload: {id, enabled}。 */
export function toggleRecipientOverride(payload) {
  return post('/api/pipelines/recipient-override/toggle', payload);
}

/** 删除一条收件人覆盖。payload: {id}。 */
export function deleteRecipientOverride(payload) {
  return post('/api/pipelines/recipient-override/delete', payload);
}

/** 清空某条服务线全部覆盖。payload: {service_id}。 */
export function resetRecipientOverride(payload) {
  return post('/api/pipelines/recipient-override/reset', payload);
}

/** 点日期改播报/休息。payload: {date:'YYYY-MM-DD', is_workday, note?}。 */
export function toggleCalendarDay(payload) {
  return post('/api/calendar/toggle', payload);
}

/** 清除当月人工裁决。payload: {month:'YYYY-MM'}。 */
export function resetCalendarMonth(payload) {
  return post('/api/calendar/reset-month', payload);
}

/** 新增一条授权。payload: {user_id, grant_type, grant_key, note?}。 */
export function addGrant(payload) {
  return post('/api/grants', payload);
}

/** 删除一条授权。payload: {user_id, grant_type, grant_key}。 */
export function deleteGrant(payload) {
  return post('/api/grants/delete', payload);
}

/** 按区域一键开通。payload: {region}。 */
export function regionGrant(payload) {
  return post('/api/grants/region', payload);
}