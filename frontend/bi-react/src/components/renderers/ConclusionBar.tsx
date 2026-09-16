/**
 * 结论条：一句话结论（内容由后端在 cube 摘要给定，前端不拼句子）。
 *
 * 定位：L1 驾驶舱顶部总结区，对应优化方案「结论条」
 * ⚠️ 组件已就绪，待后端补 `conclusion` 卡（当前后端无此卡）
 */
import type { Severity } from '../../types/cube';
interface Props {
  text: string;
  severity?: Severity;
}

export function ConclusionBar({ text, severity = 'ok' }: Props) {
  return (
    <div className="conclusion-bar" role="status">
      <span className="lead">
        <span className={`alert-chip ${severity}`}>{severity.toUpperCase()}</span>
      </span>
      <span className="metric">{text}</span>
    </div>
  );
}
