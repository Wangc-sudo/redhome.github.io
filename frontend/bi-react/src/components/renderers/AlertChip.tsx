import type { AlertInfo, Severity } from '../../types/cube';

// 四级告警 chip：色 + 文字 + 图标，色盲友好（不只靠颜色，见 VISUALIZATION §8 /
// CubeSchema §7）。颜色来自 bi-ui tokens 的 --severity-*。
const ICON: Record<Severity, string> = { p0: '⛔', p1: '⚠️', p2: 'ℹ️', ok: '✅' };

export function AlertChip({ alert }: { alert: AlertInfo }) {
  return (
    <span className={`alert-chip ${alert.severity}`} title={alert.reason}>
      <span className="alert-chip__icon" aria-hidden>
        {ICON[alert.severity]}
      </span>
      <span>{alert.label}</span>
      <span>{alert.reason}</span>
    </span>
  );
}
