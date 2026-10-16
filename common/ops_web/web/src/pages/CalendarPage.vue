<script setup>
/**
 * 播报日历（/calendar）——纯播报口径（2026-10-16 运维裁决）。
 *
 * 默认每天都是播报日（is_workday=1），休息日只来自人工裁决（点日期切换）。
 * 法定节假日不再默认休息，holiday-cn 导入已退役。
 * 读面 GET /api/calendar?month=YYYY-MM；写面 toggle / reset-month。
 * 月网格周一列起（用后端 first_weekday 计算前置空格）；徽标 裁=人工裁决。
 */
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  getCalendar,
  toggleCalendarDay,
  resetCalendarMonth,
} from '../api.js';

const route = useRoute();
const router = useRouter();

const cal = ref(null);
const loading = ref(false);
const error = ref('');
const msg = ref(null);

//: 日历源徽标（cell 角标；基线=全播报不标）。
const BADGES = {
  manual: { text: '裁', cls: 'cal-src-manual' },
};

function badge(source) {
  return BADGES[source] || null;
}

const dayHeaders = ['一', '二', '三', '四', '五', '六', '日'];

/** 周网格：前置空格 = first_weekday；后置补齐到 7 的倍数。 */
const weeks = computed(() => {
  const info = cal.value;
  if (!info) return [];
  const cells = [];
  for (let i = 0; i < info.first_weekday; i += 1) cells.push(null);
  (info.days || []).forEach((d) => cells.push(d));
  while (cells.length % 7 !== 0) cells.push(null);
  const out = [];
  for (let i = 0; i < cells.length; i += 7) out.push(cells.slice(i, i + 7));
  return out;
});

const pad = (n) => String(n).padStart(2, '0');
const monthInput = computed(() => (cal.value ? cal.value.month : ''));

/** 月度统计：播报日 / 人工裁决休息日数（可视化条）。 */
const stats = computed(() => {
  const days = (cal.value && cal.value.days) || [];
  const rest = days.filter((d) => d.is_workday === 0).length;
  return { broadcast: days.length - rest, rest, total: days.length };
});

function setMsg(type, text) {
  msg.value = { type, text };
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getCalendar(route.query.month);
    cal.value = data;
  } catch (err) {
    error.value = err.message;
    cal.value = null;
  } finally {
    loading.value = false;
  }
}

watch(() => route.query.month, load, { immediate: true });

function goMonth(month) {
  if (!month) {
    router.replace({ query: {} });
    return;
  }
  router.replace({ query: { month } });
}

function onMonthInput(event) {
  const value = event.target.value;
  if (/^\d{4}-\d{2}$/.test(value)) goMonth(value);
}

function cellClass(cell) {
  if (!cell || cell.is_workday === null) return 'cal-empty';
  return ['cal-day', cell.is_workday ? 'cal-work' : 'cal-rest',
    cell.is_today ? 'cal-today' : ''];
}

function onCell(cell) {
  if (cell && cell.is_workday !== null) onToggle(cell);
}

async function onToggle(cell) {
  const date = `${cal.value.month}-${pad(cell.day)}`;
  const willWork = !cell.is_workday;
  const action = willWork ? '改为播报' : '改为休息';
  if (!window.confirm(`确认将 ${date} ${action}？（当月日历立即重写）`)) return;
  try {
    await toggleCalendarDay({ date, is_workday: willWork, note: '' });
    setMsg('ok', `${date} 已${action}`);
    await load();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}

async function onResetMonth() {
  const month = cal.value.month;
  if (!window.confirm(
    `确认清除 ${month} 的人工裁决，恢复全播报基线（每天都是播报日）？`,
  )) return;
  try {
    await resetCalendarMonth({ month });
    setMsg('ok', `${month} 已恢复全播报基线`);
    await load();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}
</script>

<template>
  <div class="page">
    <h1>播报日历{{ cal ? `（${cal.year} 年 ${cal.month_no} 月）` : '' }}</h1>

    <div v-if="msg" class="flash" :class="msg.type">{{ msg.text }}</div>
    <div v-if="error" class="state error">{{ error }}</div>
    <div v-else-if="loading && !cal" class="state">加载中…</div>

    <template v-if="cal">
      <div class="cal-nav">
        <a href="#" @click.prevent="goMonth(cal.prev_month)">← 上一月</a>
        <strong>{{ cal.year }} 年 {{ cal.month_no }} 月</strong>
        <a href="#" @click.prevent="goMonth(cal.next_month)">下一月 →</a>
        <a href="#" @click.prevent="goMonth('')">本月</a>
        <input type="month" :value="monthInput" @change="onMonthInput">
      </div>

      <p class="hint">
        白底=播报日（默认每天播报） · 蓝底=休息（仅人工裁决） · 徽标：裁=人工裁决 ·
        点任意日期可切换 播报/休息（立即重写当月日历，全部操作留审计）
      </p>

      <div class="cal-stats">
        <span class="cal-stat">播报日 <b>{{ stats.broadcast }}</b> 天</span>
        <span class="cal-stat rest">休息 <b>{{ stats.rest }}</b> 天</span>
        <span class="cal-stat muted">全月 {{ stats.total }} 天</span>
      </div>

      <table class="cal-table">
        <tr>
          <th v-for="h in dayHeaders" :key="h">{{ h }}</th>
        </tr>
        <tr v-for="(week, wi) in weeks" :key="wi">
          <td
            v-for="(cell, ci) in week"
            :key="ci"
            :class="cellClass(cell)"
            :title="cell && cell.note ? cell.note : null"
            @click="onCell(cell)"
          >
            <template v-if="cell">
              {{ cell.day
              }}<span
                v-if="badge(cell.source)"
                class="cal-src"
                :class="badge(cell.source).cls"
              >{{ badge(cell.source).text }}</span>
            </template>
          </td>
        </tr>
      </table>

      <h2>恢复基线</h2>
      <form class="inline" @submit.prevent="onResetMonth">
        <button type="submit">清除 {{ cal.month }} 人工裁决</button>
        <span class="hint">清掉当月全部「裁」行，回到全播报基线（每天都是播报日）。</span>
      </form>

      <h2>最近修正记录</h2>
      <table>
        <tr>
          <th>时间</th><th>操作人</th><th>动作</th><th>日期</th><th>详情</th>
        </tr>
        <tr v-for="(row, i) in cal.audits" :key="i">
          <td>{{ row.created_at }}</td>
          <td>{{ row.actor }}</td>
          <td>{{ row.action_label }}</td>
          <td>{{ row.when }}</td>
          <td>{{ row.detail }}</td>
        </tr>
        <tr v-if="!cal.audits || !cal.audits.length">
          <td colspan="5" class="hint">暂无修正记录</td>
        </tr>
      </table>
    </template>
  </div>
</template>

<style scoped>
.cal-nav {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 14px 0 6px;
}
.cal-nav a {
  color: #1f3a5f;
  text-decoration: none;
  font-size: 13px;
  cursor: pointer;
}
.cal-table {
  table-layout: fixed;
  background: #fff;
}
.cal-table th {
  text-align: center;
  padding: 4px;
}
.cal-table td {
  height: 56px;
  vertical-align: top;
  padding: 4px 6px;
  font-size: 13px;
}
.cal-day {
  cursor: pointer;
}
.cal-day:hover {
  outline: 1px solid #1f3a5f;
  outline-offset: -1px;
}
.cal-rest {
  background: #e8f0fd;
  color: #1f3a5f;
}
.cal-work {
  background: #fff;
}
.cal-today {
  outline: 2px solid #1f3a5f;
  outline-offset: -2px;
}
.cal-empty {
  background: #fafafa;
  color: #c8cdd4;
}
.cal-src {
  font-size: 10px;
  padding: 0 4px;
  border-radius: 3px;
  margin-left: 4px;
}
.cal-src-manual {
  background: #ffe9c7;
  color: #8a5a00;
}
.cal-stats {
  display: flex;
  gap: 16px;
  margin: 8px 0 10px;
  font-size: 13px;
}
.cal-stat b {
  font-size: 16px;
}
.cal-stat.rest b {
  color: #1f3a5f;
}
.cal-stat.muted {
  color: #9aa0a8;
}
</style>
