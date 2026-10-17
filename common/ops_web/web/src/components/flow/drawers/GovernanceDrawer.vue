<script setup>
/**
 * 治理报告抽屉（2026-10-17 舰队治理 P1）：停用盘点/僵尸/装饰依赖线/cron 堆积。
 * 纯展示（计算在 flow/governance.js）；用户裁决：治理产出不发群播报，
 * 只在画布看板。
 */
import { computed } from 'vue';
import { governanceReport } from '../../../flow/governance.js';

const props = defineProps({
  groups: { type: Array, default: () => [] },
});

const today = new Date().toISOString().slice(0, 10);
const report = computed(() => governanceReport(props.groups, today));
</script>

<template>
  <div>
    <h2>舰队治理报告</h2>
    <p class="drawer-sub">
      共 {{ report.total }} 条 · 启用 {{ report.enabled }} · 停用 {{ report.disabled.length }}
      · 僵尸 {{ report.zombies.length }} · 装饰依赖 {{ report.decorativeDeps.length }}
      （{{ today }} 口径，只展示不播报）
    </p>

    <div class="drawer-sec">
      <h3>停用盘点（{{ report.disabled.length }}）</h3>
      <div v-for="row in report.disabled" :key="row.service_id" class="gov-row">
        <span class="gov-id">{{ row.service_id }}</span>
        <span v-if="row.overdue" class="gov-overdue">复查逾期</span>
        <span v-else-if="row.review_after" class="gov-review">复查 {{ row.review_after }}</span>
        <span class="gov-detail">{{ row.reason || '（原因缺失）' }}</span>
      </div>
      <p v-if="!report.disabled.length" class="drawer-line muted">无停用任务</p>
    </div>

    <div class="drawer-sec">
      <h3>僵尸任务（启用但 7 天未跑，{{ report.zombies.length }}）</h3>
      <div v-for="row in report.zombies" :key="row.service_id" class="gov-row">
        <span class="gov-id">{{ row.service_id }}</span>
        <span class="gov-detail">最近 {{ row.last }}</span>
      </div>
      <p v-if="!report.zombies.length" class="drawer-line muted">无</p>
    </div>

    <div class="drawer-sec">
      <h3>装饰依赖线（上游无探针，门禁不生效，{{ report.decorativeDeps.length }}）</h3>
      <div v-for="row in report.decorativeDeps" :key="row.service_id" class="gov-row">
        <span class="gov-id">{{ row.service_id }}</span>
        <span class="gov-detail">依赖 {{ row.deps.join('、') }}</span>
      </div>
      <p v-if="!report.decorativeDeps.length" class="drawer-line muted">无</p>
    </div>

    <div class="drawer-sec">
      <h3>cron 同时刻堆积（≥3 条，{{ report.pileups.length }}）</h3>
      <div v-for="row in report.pileups" :key="row.time" class="gov-row">
        <span class="gov-id">{{ row.time }} × {{ row.count }}</span>
        <span class="gov-detail">{{ row.services.join('、') }}</span>
      </div>
      <p v-if="!report.pileups.length" class="drawer-line muted">无</p>
    </div>
  </div>
</template>

<style scoped>
.gov-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  padding: 5px 0;
  border-bottom: 1px solid #f3f4f6;
  font-size: 12px;
}
.gov-id {
  font-weight: 600;
  color: #1f2933;
}
.gov-overdue {
  color: #b91c1c;
  background: #fef2f2;
  border-radius: 8px;
  padding: 0 6px;
}
.gov-review {
  color: #92400e;
}
.gov-detail {
  width: 100%;
  color: #6b7280;
}
</style>
