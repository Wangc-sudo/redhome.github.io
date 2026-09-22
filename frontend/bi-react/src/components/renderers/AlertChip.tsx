/**
 * 四级告警 chip：色 + 文字 + 图标，色盲友好（不只靠颜色）。
 *
 * 颜色来源：bi-ui tokens.css 的 --severity-p0/p1/p2/ok（已在 CubeSchema §7 验证 WCAG AA）
 * 图标：p0=⛔ p1=⚠️ p2=ℹ️ ok=✅（与颜色成组，单独靠颜色时仍有辨识度）
 * 语义：告警级别由后端 derived.py 算出，本组件只做展示（无任何算式）
 */
import type { AlertInfo, Severity } from '../../types/cube';
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
