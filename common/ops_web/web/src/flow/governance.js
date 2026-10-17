/**
 * 舰队治理报告计算（2026-10-17 治理 P1，纯函数可单测）。
 *
 * 输入 = /api/pipelines 的 groups[].services[]；输出四节体检结果。
 * 全部画布内展示（用户裁决：治理产出不发群播报）。
 */

//: 有摘要探针的上游（依赖门禁真生效）；未登记的依赖=装饰线（按已满足处理）。
const PROBED_UPSTREAMS = new Set([
  'roll-manifest',
  'sync-channel-sales',
  'extract-channel',
  'extract-mart',
]);

//: 僵尸判定阈值（启用但最近运行超过 N 天）。
const ZOMBIE_DAYS = 7;

function flattenServices(groups) {
  const out = [];
  for (const group of groups || []) {
    for (const service of group.services || []) out.push(service);
  }
  return out;
}

/** 停用盘点：带原因/复查日，复查过期（review_after < 今天）置顶标红。 */
export function disabledAudit(services, today) {
  const rows = [];
  for (const s of services) {
    if (s.enabled) continue;
    rows.push({
      service_id: s.service_id,
      label: s.service_label || s.service_id,
      reason: s.disabled_reason || '',
      review_after: s.review_after || '',
      overdue: !!(s.review_after && s.review_after < today),
    });
  }
  rows.sort((a, b) => Number(b.overdue) - Number(a.overdue)
    || a.service_id.localeCompare(b.service_id));
  return rows;
}

/** 僵尸检测：启用但无运行记录，或最近运行超过 ZOMBIE_DAYS 天。 */
export function zombieAudit(services, today, days = ZOMBIE_DAYS) {
  const rows = [];
  for (const s of services) {
    if (!s.enabled) continue;
    const latest = s.latest || null;
    const startedAt = latest && latest.started_at;
    if (!startedAt) {
      rows.push({ service_id: s.service_id, label: s.service_label || s.service_id, last: '无记录' });
      continue;
    }
    const ageDays = Math.floor(
      (Date.parse(`${today}T00:00:00Z`) - Date.parse(startedAt.replace(' ', 'T')))
      / 86400000,
    );
    if (ageDays > days) {
      rows.push({
        service_id: s.service_id,
        label: s.service_label || s.service_id,
        last: startedAt,
        ageDays,
      });
    }
  }
  rows.sort((a, b) => (b.ageDays || 9999) - (a.ageDays || 9999));
  return rows;
}

/** 装饰依赖线：depends_on 引用未注册探针的上游（门禁不生效，纯装饰）。 */
export function decorativeDeps(services) {
  const rows = [];
  for (const s of services) {
    const fake = (s.depends_on || []).filter((d) => !PROBED_UPSTREAMS.has(d));
    if (fake.length) {
      rows.push({ service_id: s.service_id, label: s.service_label || s.service_id, deps: fake });
    }
  }
  return rows;
}

/** cron 堆积：同一时刻（HH:MM）启用的任务数 > 阈值。 */
export function cronPileup(services, threshold = 3) {
  const byTime = {};
  for (const s of services) {
    if (!s.enabled) continue;
    const zh = s.schedule_zh || '';
    for (const m of zh.matchAll(/(\d{1,2}):(\d{2})/g)) {
      const key = `${m[1].padStart(2, '0')}:${m[2]}`;
      if (!byTime[key]) byTime[key] = [];
      byTime[key].push(s.service_id);
    }
  }
  return Object.entries(byTime)
    .filter(([, ids]) => ids.length >= threshold)
    .map(([time, ids]) => ({ time, count: ids.length, services: ids }))
    .sort((a, b) => b.count - a.count);
}

/** 总报告装配。 */
export function governanceReport(groups, today) {
  const services = flattenServices(groups);
  return {
    total: services.length,
    enabled: services.filter((s) => s.enabled).length,
    disabled: disabledAudit(services, today),
    zombies: zombieAudit(services, today),
    decorativeDeps: decorativeDeps(services),
    pileups: cronPileup(services),
  };
}
