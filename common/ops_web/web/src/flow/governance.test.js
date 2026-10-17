/**
 * flow/governance.js 单测（2026-10-17 治理 P1）。
 */
import { describe, expect, it } from 'vitest';
import {
  cronPileup,
  decorativeDeps,
  disabledAudit,
  governanceReport,
  zombieAudit,
} from './governance.js';

const TODAY = '2026-10-17';

function svc(id, rest = {}) {
  return {
    service_id: id, service_label: id, enabled: true,
    depends_on: [], schedule_zh: '', latest: null, ...rest,
  };
}

function groupsOf(...services) {
  return [{ region: 'x', services }];
}

describe('disabledAudit', () => {
  it('停用带原因；复查过期置顶', () => {
    const rows = disabledAudit([
      svc('b', { enabled: false, disabled_reason: 'r', review_after: '2026-11-01' }),
      svc('a', { enabled: false, disabled_reason: 'r', review_after: '2026-10-01' }),
      svc('c', { enabled: true }),
    ], TODAY);
    expect(rows).toHaveLength(2);
    expect(rows[0].service_id).toBe('a');          // 过期置顶
    expect(rows[0].overdue).toBe(true);
    expect(rows[1].overdue).toBe(false);
  });
});

describe('zombieAudit', () => {
  it('启用但无记录=僵尸；超龄=僵尸（真源=健康读面）', () => {
    const rows = zombieAudit([
      svc('norun'),
      svc('fresh'),
      svc('stale'),
      svc('off', { enabled: false }),
    ], TODAY, undefined, {
      fresh: { started_at: '2026-10-16 08:30:00' },
      stale: { started_at: '2026-10-01 08:30:00' },
    });
    expect(rows.map((r) => r.service_id).sort()).toEqual(['norun', 'stale']);
  });
  it('周期宽限（2026-10-17 用户裁决）：月检/周检不按日检误报', () => {
    const rows = zombieAudit([
      // 月检从未跑过：首期未到，不算僵尸
      svc('calendar-watch', { schedule_zh: '每月 25日 10:00' }),
      // 周检 10 天没跑：宽限 9 天，超了 → 僵尸
      svc('weekly-x', { schedule_zh: '每周一 09:30' }),
      // 日检 8 天没跑 → 僵尸
      svc('daily-x', { schedule_zh: '每天 08:30' }),
    ], TODAY, undefined, {
      // 宽限边界留足余量（UTC/本地时区混合解析会差一天）
      'weekly-x': { started_at: '2026-10-06 09:30:00' },   // 约 11 天 > 9
      'daily-x': { started_at: '2026-10-08 08:30:00' },    // 约 9 天 > 7
    });
    expect(rows.map((r) => r.service_id).sort()).toEqual(['daily-x', 'weekly-x']);
  });
  it('service.latest（手动运行请求表）不参与僵尸判定', () => {
    // 回归（2026-10-17 误报事故）：latest 有手动运行 ≠ 真实 cron 运行
    const rows = zombieAudit([
      svc('manual-only', { latest: { started_at: '2026-10-16 08:30:00' } }),
    ], TODAY, undefined, {});
    expect(rows.map((r) => r.service_id)).toEqual(['manual-only']);
  });
});

describe('decorativeDeps', () => {
  it('未注册探针的上游=装饰线；探针上游不算', () => {
    const rows = decorativeDeps([
      svc('a', { depends_on: ['extract-mart'] }),
      svc('b', { depends_on: ['robot-hangzhou'] }),
      svc('c', { depends_on: ['extract-mart', 'robot-x'] }),
    ]);
    expect(rows).toEqual([
      { service_id: 'b', label: 'b', deps: ['robot-hangzhou'] },
      { service_id: 'c', label: 'c', deps: ['robot-x'] },
    ]);
  });
});

describe('cronPileup', () => {
  it('同时刻启用任务 ≥3 才报', () => {
    const rows = cronPileup([
      svc('a', { schedule_zh: '每天 08:30' }),
      svc('b', { schedule_zh: '每天 08:30' }),
      svc('c', { schedule_zh: '每天 08:30' }),
      svc('d', { schedule_zh: '每天 18:00' }),
      svc('e', { enabled: false, schedule_zh: '每天 08:30' }),  // 停用不计
    ]);
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ time: '08:30', count: 3 });
  });
});

describe('governanceReport', () => {
  it('四节装配 + 总数/启用计数', () => {
    const report = governanceReport(groupsOf(
      svc('a'), svc('b', { enabled: false, disabled_reason: 'r' }),
    ), TODAY, { a: { started_at: '2026-10-17 08:30:00' } });
    expect(report.total).toBe(2);
    expect(report.enabled).toBe(1);
    expect(report.disabled).toHaveLength(1);
    expect(report.zombies).toHaveLength(0);       // a 今早已跑
  });
});
