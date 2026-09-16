/**
 * React 错误边界：等价材料的 componentDidCatch。
 *
 * 职责：单卡渲染崩溃时，只显示该卡的降级 UI，不拖垮整板。
 * 上报：componentDidCatch 里预留上报钩子（接错误监控，见 ARCHITECTURE.md §6）。
 * ⚠️ TODO：接入监控系统（当前仅 console.error）
 */
import React from 'react';
// 单卡渲染崩溃时，只显示该卡的降级 UI，不拖垮整板。
// componentDidCatch 里预留上报钩子（接错误监控，见 ARCHITECTURE.md §6）。
interface Props {
  children: React.ReactNode;
  fallback?: React.ReactNode;
  name?: string;
}
interface State {
  hasError: boolean;
}

export class ErrorBoundary extends React.Component<Props, State> {
  state: State = { hasError: false };

  static getDerivedStateFromError(): State {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // TODO: 上报到监控系统（保留 componentDidCatch 等价能力）
    console.error(`[ErrorBoundary:${this.props.name ?? 'unknown'}]`, error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      return this.props.fallback ?? <div className="card card--error">组件渲染失败</div>;
    }
    return this.props.children;
  }
}
