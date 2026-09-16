/**
 * 三态组件：复用 bi-ui/components.css 的 .loading / .empty / .page-error。
 *
 * 状态覆盖：
 *   - LoadingState：aria-busy="true"，告知无障碍工具
 *   - EmptyState：数据为空（可能是看板无卡、后端无数据）
 *   - ErrorState：BiWebError 走 toMessage()，其他走 error.message；支持重试回调
 *
 * 与 V1 的 loading/empty/error 体系对齐，保证状态覆盖一致。
 */
import { BiWebError } from '../../data/errors';

export function LoadingState({ label = '加载中…' }: { label?: string }) {
  return (
    <div className="card loading" aria-busy="true">
      {label}
    </div>
  );
}

export function EmptyState({ label = '暂无数据' }: { label?: string }) {
  return <div className="card empty">{label}</div>;
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  const message = error instanceof BiWebError ? error.toMessage() : error.message;
  return (
    <div className="card page-error" role="alert">
      <p>{message}</p>
      {onRetry && (
        <button className="btn" onClick={onRetry} style={{ marginTop: 8 }}>
          重试
        </button>
      )}
    </div>
  );
}
