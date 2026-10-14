<script setup>
/**
 * 工作日历（/calendar）——迁移自旧 SSR 页面（保真：app.py 旧 calendar_page + _PAGE_JS）。
 *
 * 读面 GET /api/calendar?month=YYYY-MM；写面 toggle / import-year / reset-month。
 * 月网格周一列起（用后端 first_weekday 计算前置空格）；三源徽标 法/裁/兜。
 */
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  getCalendar,
  toggleCalendarDay,
  importCalendarYear,
  resetCalendarMonth,
} from '../api.js';

const route = useRoute();
const router = useRouter();

const cal = ref(null);
const loading = ref(false);
const error = ref('');
const msg = ref(null);
const importYear = ref('');

//: 日历源徽标（cell 角标；local=种子/规则基线不标）——与后端 _CAL_SOURCE_BADGE 对齐。
const BADGES = {
  holiday_cn: { text: '法', cls: 'cal-src-holiday_cn' },
  manual: { text: '裁', cls: 'cal-src-manual' },
  rule_fallback: { text: '兜', cls: 'cal-src-rule_fallback' },
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
function setMsg(type, text) {
  msg.value = { type, text };
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getCalendar(route.query.month);
    cal.value = data;
    importYear.value = String(data.month_no >= 10 ? data.year + 1 : data.year);
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
  const action = willWork ? '改为上班' : '改为休息';
  if (!window.confirm(`确认将 ${date} ${action}？（当月日历立即重写）`)) return;
  try {
    await toggleCalendarDay({ date, is_workday: willWork, note: '' });
    setMsg('ok', `${date} 已${action}`);
    await load();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}

async function onImportYear() {
  const year = Number(importYear.value);
  if (!Number.isInteger(year)) {
    setMsg('err', '请输入合法年份');
    return;
  }
  if (!window.confirm(
    `确认导入 ${year} 年法定节假日？（该年法定导入行整年替换，人工裁决不动；`
    + '随后按当前口径重写全年日历）',
  )) return;
  try {
    await importCalendarYear({ year });
    setMsg('ok', `${year} 年法定假日已导入`);
    await load();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}

async function onResetMonth() {
  const month = cal.value.month;
  if (!window.confirm(
    `确认清除 ${month} 的人工裁决（法定假日导入保留），恢复规则口径？`,
  )) return;
  try {
    await resetCalendarMonth({ month });
    setMsg('ok', `${month} 已恢复规则口径`);
    await load();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}
</script>

<template>
  <div class="page">
    <h1>工作日历{{ cal ? `（${cal.year} 年 ${cal.month_no} 月）` : '' }}</h1>

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
        蓝底=休息 · 白底=上班 · 徽标：法=法定节假日导入 / 裁=人工裁决 / 兜=规则兜底
        （种子未覆盖月，法定假未知按工作日计） · 点任意日期可切换 上班/休息
        （立即重写当月日历，全部操作留审计）
      </p>

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

      <h2>导入法定节假日（holiday-cn 官方转录）</h2>
      <form class="inline" @submit.prevent="onImportYear">
        <input v-model="importYear" type="number" min="2020" max="2100" required>
        <button type="submit">一键导入该年法定假日</button>
        <span class="hint">
          该年法定导入行整年替换，人工裁决（裁）不动；随后按当前口径重写全年日历。
          国务院一般 10-11 月发布次年安排。
        </span>
      </form>

      <h2>恢复规则</h2>
      <form class="inline" @submit.prevent="onResetMonth">
        <button type="submit">清除 {{ cal.month }} 人工裁决</button>
        <span class="hint">只清「裁」行（法定导入「法」保留），当月回到规则基线口径。</span>
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
.cal-src-holiday_cn {
  background: #d8e8ff;
  color: #1f3a5f;
}
.cal-src-manual {
  background: #ffe9c7;
  color: #8a5a00;
}
.cal-src-rule_fallback {
  background: #ececf0;
  color: #777;
}
</style>
