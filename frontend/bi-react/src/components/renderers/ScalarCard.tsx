/**
 * KPI 卡：吃标量 CubeSchema（rows[0] + derived），只渲染不算。
 *
 * 数据流：
 *   CardPayload.scalar → cardToCube(fromScalar) → CubeSchema{chart:'scalar'}
 *   → derived['card_id'] = { target, done, progressRate, delta, missing }
 *
 * 展示规则：
 *   - value == null → 显示「—」
 *   - progressRate → 进度条（封顶 100%，原值保留用于告警）
 *   - delta → 环比方向色（涨红跌绿）
 *   - confidence:'low' → 低置信标注
 */
import type { CubeSchema } from '../../types/cube';
import { deltaClass, formatDelta, formatPct, scaleMoney } from '../../utils/format';
// 缺失 → 显示「—」且不渲染数字；环比方向色由 .delta.up/.down 决定（涨红跌绿）。
export function ScalarCard({ cube }: { cube: CubeSchema }) {
  const key = cube.rowKeys?.[0];
  const derived = key ? cube.derived?.[key] : undefined;
  const raw = cube.rows[0]?.value;
  const value = typeof raw === 'number' ? raw : Number.isFinite(Number(raw)) ? Number(raw) : null;
  const scaled = value == null ? null : scaleMoney(value, cube.unit);

  return (
    <div className="card">
      <div className="card-title">{cube.title}</div>
      <div className="card-body kpi">
        {scaled == null || derived?.missing ? (
          <div className="kpi-value">—</div>
        ) : (
          <div className="kpi-value">
            {scaled.text}
            {scaled.unit && <span className="kpi-sub"> {scaled.unit}</span>}
          </div>
        )}

        {cube.asOf && <div className="kpi-sub">数据截至 {cube.asOf}</div>}

        {derived?.delta != null && (
          <div className="kpi-sub">
            环比 <span className={deltaClass(derived.delta)}>{formatDelta(derived.delta)}</span>
          </div>
        )}

        {derived?.progressRate != null && (
          <>
            <div className="progress">
              {/* 仅显示封顶（CubeSchema §2.2），不改变原值语义 */}
              <div className="progress-bar" style={{ width: `${Math.min(derived.progressRate * 100, 100)}%` }} />
            </div>
            <div className="kpi-sub">达成 {formatPct(derived.progressRate)}</div>
          </>
        )}

        {derived?.confidence === 'low' && <div className="kpi-sub">样本不足 · 低置信</div>}
      </div>
    </div>
  );
}
