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
  it('启用但无记录=僵尸；超龄=僵尸', () => {
    const rows = zombieAudit([
      svc('norun'),
      svc('fresh', { latest: { started_at: '2026-10-16 08:30:00' } }),
      svc('stale', { latest: { started_at: '2026-10-01 08:30:00' } }),
      svc('off', { enabled: false }),
    ], TODAY);
    expect(rows.map((r) => r.service_id).sort()).toEqual(['norun', 'stale']);
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
    ), TODAY);
    expect(report.total).toBe(2);
    expect(report.enabled).toBe(1);
    expect(report.disabled).toHaveLength(1);
    expect(report.zombies).toHaveLength(1);       // a 无运行记录
  });
});
