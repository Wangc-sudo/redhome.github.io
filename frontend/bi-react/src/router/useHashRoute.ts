/** hash 路由：`#/d/{dashboardId}`（ARCHITECTURE §9 已约定 hash，避免服务端配置）。 */
import { useCallback, useEffect, useState } from 'react';

const HASH_RE = /^#?\/?d\/([^/?#]+)/;

/** '#/d/l1-cockpit' → 'l1-cockpit'；不匹配返回 null。 */
export function parseHash(hash: string): string | null {
  const m = HASH_RE.exec(hash.trim());
  return m ? decodeURIComponent(m[1]) : null;
}

export function hrefFor(id: string): string {
  return `#/d/${encodeURIComponent(id)}`;
}

/**
 * 当前看板 id + 跳转函数。刷新/分享 URL 都能还原页面（旧实现存 zustand，刷新回首页）。
 */
export function useHashRoute(): [string | null, (id: string) => void] {
  const [id, setId] = useState<string | null>(() => parseHash(window.location.hash));

  useEffect(() => {
    const onHash = () => setId(parseHash(window.location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  const navigate = useCallback((next: string) => {
    const href = hrefFor(next);
    if (window.location.hash === href) return;
    window.location.hash = href; // 触发 hashchange → setId
  }, []);

  return [id, navigate];
}
