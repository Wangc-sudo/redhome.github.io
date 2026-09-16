// @vitest-environment jsdom
/**
 * /d/{id} 初始路由兜底（T5：bi-web 壳替换为 React dist 后，旧书签 /d/l2-region
 * 无 hash 直访也必须落到对应看板）。机制不变：初始值取一次，之后仍由 hashchange 驱动。
 */
import { act, renderHook } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';
import { useHashRoute } from './useHashRoute';

afterEach(() => {
  window.history.replaceState(null, '', '/');
});

describe('useHashRoute · /d/{id} 初始路由兜底', () => {
  it('hash 为空且 pathname = /d/{id} → 初始路由取该 id（旧书签直达不丢）', () => {
    window.history.replaceState(null, '', '/d/l2-region');
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toBe('l2-region');
  });

  it('hash 优先：#/d/l1-cockpit 压过 pathname /d/l2-region', () => {
    window.history.replaceState(null, '', '/d/l2-region#/d/l1-cockpit');
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toBe('l1-cockpit');
  });

  it('hash 为空但 pathname 不匹配 /d/{id} → 初始为 null（回默认看板）', () => {
    window.history.replaceState(null, '', '/api/v1/dashboards');
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toBeNull();
  });

  it('初始兜底之后仍由 hashchange 驱动（机制不变）', () => {
    window.history.replaceState(null, '', '/d/l2-region');
    const { result } = renderHook(() => useHashRoute());
    expect(result.current[0]).toBe('l2-region');

    act(() => {
      window.location.hash = '#/d/l2-channel';
      window.dispatchEvent(new HashChangeEvent('hashchange'));
    });
    expect(result.current[0]).toBe('l2-channel');
  });
});
