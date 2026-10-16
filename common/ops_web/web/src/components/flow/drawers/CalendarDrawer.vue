<script>
// 模块级：跨开关记住上次查看的月份（等价旧页内 calMonth ref 的会话级留存）
const lastCalMonth = { value: '' };
</script>

<script setup>
/**
 * 播报日历抽屉（2026-10-16 F2 从 FlowPage.vue 抽出）。
 * 自取自绘：开抽屉读当月（或上次查看月），点日期=切换 播报/休息（人工裁决，
 * 留审计），可清除当月裁决回全播报基线；全部抽屉内闭环，不碰画布数据。
 */
import { computed, onMounted, ref } from 'vue';
import {
  getCalendar,
  resetCalendarMonth,
  toggleCalendarDay,
} from '../../../api.js';
import { setFlash } from '../../../store/me.js';

defineProps({
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(['close', 'update:busy']);

const calData = ref(null);
const calMonth = ref('');

async function loadCalendar(month) {
  emit('update:busy', true);
  try {
    calData.value = await getCalendar(month);
    calMonth.value = calData.value.month;
    lastCalMonth.value = calMonth.value;
  } catch (err) {
    setFlash('err', `日历读取失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

const calCells = computed(() => {
  if (!calData.value) return [];
  const cells = [];
  for (let i = 0; i < (calData.value.first_weekday || 0); i += 1) cells.push(null);
  for (const day of calData.value.days || []) cells.push(day);
  return cells;
});

async function onToggleCalDay(cell) {
  if (!cell) return;
  const date = `${calData.value.month}-${String(cell.day).padStart(2, '0')}`;
  const willWork = !cell.is_workday;
  emit('update:busy', true);
  try {
    await toggleCalendarDay({ date, is_workday: willWork, note: '' });
    setFlash('ok', `${date} 已改为${willWork ? '播报' : '休息'}`);
    await loadCalendar(calMonth.value);
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onResetCalMonth() {
  if (!window.confirm(`清除 ${calMonth.value} 的人工裁决，回到全播报基线？`)) return;
  emit('update:busy', true);
  try {
    await resetCalendarMonth({ month: calMonth.value });
    setFlash('ok', `${calMonth.value} 已恢复全播报基线`);
    await loadCalendar(calMonth.value);
  } catch (err) {
    setFlash('err', `操作失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

onMounted(() => loadCalendar(lastCalMonth.value || undefined));
</script>

<template>
  <h2>播报日历</h2>
  <p class="drawer-sub">默认每天播报；点日期=切换 播报/休息（人工裁决，留审计）</p>
  <div class="drawer-sec" v-if="calData">
    <div class="cal-nav">
      <button class="btn mini" @click="loadCalendar(calData.prev_month)">← 上月</button>
      <b>{{ calData.year }} 年 {{ calData.month_no }} 月</b>
      <button class="btn mini" @click="loadCalendar(calData.next_month)">下月 →</button>
    </div>
    <div class="cal-grid">
      <span v-for="w in ['一','二','三','四','五','六','日']" :key="w" class="cal-wd">{{ w }}</span>
      <template v-for="(cell, i) in calCells" :key="i">
        <span v-if="!cell" class="cal-cell empty" />
        <button
          v-else
          class="cal-cell"
          :class="{ rest: !cell.is_workday, manual: cell.source === 'manual' }"
          :title="`${cell.day} 日 ${cell.is_workday ? '播报' : '休息'}${cell.note ? ' · ' + cell.note : ''}`"
          @click="onToggleCalDay(cell)"
        >{{ cell.day }}</button>
      </template>
    </div>
    <p class="drawer-line muted">
      播报日 {{ calData.days.filter((d) => d.is_workday).length }} 天 ·
      休息 {{ calData.days.filter((d) => !d.is_workday).length }} 天
    </p>
    <button class="btn mini" :disabled="busy" @click="onResetCalMonth">
      清除本月人工裁决（回全播报）
    </button>
  </div>
</template>
