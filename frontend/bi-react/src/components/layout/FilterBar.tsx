/**
 * 筛选条：筛选项与候选值都来自后端（dashboard.filters + /api/v1/options/{source}）。
 *
 * 数据流：
 *   看板定义 def.filters → FilterBar
 *   → 每个 FilterSelect 独立调 useFilterOptions(source) → /api/v1/options/{source}
 *
 * 规则：每个 select 独立订阅共享缓存，避免在父组件循环调 hook（违反 Rule of Hooks）
 */
import { useFilterOptions } from '../../data/useDashboard';
import type { FilterDef } from '../../data/types';
// 每个 select 独立订阅共享缓存，避免在父组件循环调 hook。
function FilterSelect({
  filter,
  value,
  onChange,
}: {
  filter: FilterDef;
  value: string;
  onChange: (v: string) => void;
}) {
  const { data: options, loading } = useFilterOptions(filter.source);

  return (
    <label className="filter">
      <span className="filter-label">{filter.label}</span>
      <select value={value} onChange={(e) => onChange(e.target.value)} disabled={loading && !options}>
        <option value="">全部</option>
        {(options ?? []).map((o) => (
          <option key={o} value={o}>
            {o}
          </option>
        ))}
      </select>
    </label>
  );
}

interface Props {
  filters: FilterDef[];
  value: Record<string, string>;
  onChange: (param: string, v: string) => void;
}

export function FilterBar({ filters, value, onChange }: Props) {
  if (filters.length === 0) return null;
  return (
    <div className="filter-band">
      {filters.map((f) => (
        <FilterSelect key={f.param} filter={f} value={value[f.param] ?? ''} onChange={(v) => onChange(f.param, v)} />
      ))}
    </div>
  );
}
