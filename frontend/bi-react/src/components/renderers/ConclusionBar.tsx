import type { Severity } from '../../types/cube';

// 结论条：一句话结论（内容由后端在 cube 摘要给定，前端不拼句子）。
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
