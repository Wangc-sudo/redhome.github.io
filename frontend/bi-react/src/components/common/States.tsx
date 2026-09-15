// 三态：复用 bi-ui/components.css 的 .loading / .empty / .page-error。
// 与 V1 的 loading(aria-busy)/empty/error 体系对齐，保证状态覆盖一致。
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
