<script setup>
/**
 * 操作记录抽屉（2026-10-16 F2 从 FlowPage.vue 抽出）：
 * 六源聚合审计（授权/定时任务/名册/播报日历/收件人/定时运行），
 * 来源/操作人过滤。自取自绘，抽屉内闭环。
 */
import { onMounted, ref } from 'vue';
import { getRecords } from '../../../api.js';
import { setFlash } from '../../../store/me.js';

defineProps({
  busy: { type: Boolean, default: false },
});
const emit = defineEmits(['close', 'update:busy']);

const recRows = ref([]);
const recSources = ref([]);
const recFilter = ref({ source: '', actor: '' });

async function loadRecords() {
  emit('update:busy', true);
  try {
    const data = await getRecords({
      source: recFilter.value.source || undefined,
      actor: recFilter.value.actor || undefined,
    });
    recRows.value = (data && data.records) || [];
    recSources.value = (data && data.sources) || [];
  } catch (err) {
    setFlash('err', `记录读取失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

onMounted(loadRecords);
</script>

<template>
  <h2>操作记录</h2>
  <p class="drawer-sub">六源聚合审计（授权/定时任务/名册/播报日历/收件人/定时运行）</p>
  <div class="drawer-sec">
    <div class="rec-filter">
      <select v-model="recFilter.source" class="roster-select" @change="loadRecords">
        <option value="">全部来源</option>
        <option v-for="s in recSources" :key="s" :value="s">{{ s }}</option>
      </select>
      <input
        v-model="recFilter.actor"
        placeholder="操作人"
        class="roster-input"
        @change="loadRecords"
      >
    </div>
    <div v-for="(r, i) in recRows" :key="i" class="rec-row">
      <span class="rec-time">{{ r.created_at }}</span>
      <span class="rec-source">{{ r.source }}</span>
      <span class="rec-action">{{ r.action_label }}</span>
      <span class="rec-target">{{ r.actor }} → {{ r.target }}</span>
      <span class="rec-detail">{{ r.detail }}</span>
    </div>
    <p v-if="!recRows.length" class="drawer-line muted">无记录</p>
  </div>
</template>
