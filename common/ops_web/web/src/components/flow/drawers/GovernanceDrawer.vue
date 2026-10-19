<script setup>
/**
 * 治理报告抽屉（2026-10-17 舰队治理 P1）：停用盘点/僵尸/装饰依赖线/cron 堆积。
 * 纯展示（计算在 flow/governance.js）；用户裁决：治理产出不发群播报，
 * 只在画布看板。
 */
import { computed, onMounted, ref } from 'vue';
import { getFleetAuditLatest, getPipelinesHealth, getPipelinesSla } from '../../../api.js';
import { governanceReport } from '../../../flow/governance.js';

const props = defineProps({
  groups: { type: Array, default: () => [] },
});

const today = new Date().toISOString().slice(0, 10);
// 僵尸检测真源=真实 cron 运行史（勿用 service.latest——手动请求表）
const health = ref({});
//: 上次月检快照（2026-10-17 治理 P2：fleet-audit 管道每月 2 日落库，
//: 只进本抽屉不发群）；null=加载中，undefined=读取失败（静默降级）。
const snapshot = ref(null);
//: 当日 SLA 迟到（2026-10-19 治理 P2 尾巴，服务端判定前端纯渲染，
//: 用户裁决「不要播报」）；null=加载中/失败（静默降级）。
const sla = ref(null);
onMounted(async () => {
  try {
    const data = await getPipelinesHealth();
    health.value = (data && data.latest) || {};
  } catch { health.value = {}; }
  try {
    const data = await getFleetAuditLatest();
    snapshot.value = (data && data.snapshot) || undefined;
  } catch { snapshot.value = undefined; }
  try {
    sla.value = await getPipelinesSla();
  } catch { sla.value = null; }
});
const report = computed(() => governanceReport(props.groups, today, health.value));
const snapshotReport = computed(() => (snapshot.value && snapshot.value.report) || null);
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

    <div v-if="snapshot === undefined" class="drawer-sec">
      <h3>上次月检快照</h3>
      <p class="drawer-line muted">尚未跑过（fleet-audit 每月 2 日 07:00）</p>
    </div>
    <div v-if="snapshotReport" class="drawer-sec">
      <h3>上次月检快照（{{ snapshot.audit_date }}）</h3>
      <p class="drawer-line muted">
        舰队 {{ snapshotReport.total }} 条 · 启用 {{ snapshotReport.enabled }}
        · 停用 {{ snapshotReport.disabled.length }}
        · 僵尸 {{ snapshotReport.zombies.length }}
        · 装饰依赖 {{ snapshotReport.decorative_deps.length }}
        · cron 堆积 {{ snapshotReport.pileups.length }}
      </p>
      <div v-for="row in snapshotReport.zombies" :key="row.service_id" class="gov-row">
        <span class="gov-id">{{ row.service_id }}</span>
        <span class="gov-detail">僵尸：{{ row.last ? `最近 ${row.last}` : '无运行记录' }}</span>
      </div>
      <div v-for="row in snapshotReport.disabled.filter((r) => r.overdue)" :key="row.service_id" class="gov-row">
        <span class="gov-id">{{ row.service_id }}</span>
        <span class="gov-overdue">复查逾期</span>
        <span class="gov-detail">{{ row.reason || '（原因缺失）' }}</span>
      </div>
    </div>

    <div v-if="sla" class="drawer-sec">
      <h3>SLA 迟到（当日 {{ sla.checked_at }} 口径，{{ sla.violations.length }}）</h3>
      <div v-for="row in sla.violations" :key="row.service_id" class="gov-row">
        <span class="gov-id">{{ row.service_id }}</span>
        <span class="gov-overdue">迟到</span>
        <span class="gov-detail">应于 {{ row.deadline }} 前完成当日运行</span>
      </div>
      <p v-if="!sla.violations.length" class="drawer-line muted">无</p>
    </div>

    <div class="drawer-sec">
      <h3>僵尸任务（启用但超周期宽限未跑，{{ report.zombies.length }}）</h3>
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
