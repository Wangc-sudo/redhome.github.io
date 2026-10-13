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
    if (resp.status === 401 || resp.status === 403) {
      throw new Error('登录已过期或无权限，请重新登录');
    }
    throw new Error(readableDetail(data && data.detail, resp.status));
  }
  return data;
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