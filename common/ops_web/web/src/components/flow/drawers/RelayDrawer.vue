<script setup>
/**
 * 播报试投抽屉（2026-10-19 用户裁决：测试区改制为通用功能模块，与播报
 * 日历同级——它只是播报试投出口，不是业务区域）。
 *
 * 集中管理全区域的「播报改投测试区」开关（写 dim_org_region.relay_to，
 * 网关 30 秒内生效，无需重启）；改投消息带 [测试→原区域] 前缀；变更
 * 流水见「操作记录」（org 目录审计源已含 set_relay）。
 */
import { computed } from 'vue';
import { setRegionRelay } from '../../../api.js';
import { setFlash } from '../../../store/me.js';

const props = defineProps({
  regionNodes: { type: Array, default: () => [] },  // 区域节点（携带 relayTo）
  busy: { type: Boolean, default: false },
  reload: { type: Function, required: true },
});
const emit = defineEmits(['close', 'update:busy']);

// 可改投区域：业务区域节点（聚合圆节点与平台线除外）
const regions = computed(() => props.regionNodes
  .filter((n) => !n.isAggregate && !n.isPlatform)
  .map((n) => ({ key: n.key, label: n.label, relayTo: n.relayTo || null })));

const relayedCount = computed(() =>
  regions.value.filter((r) => r.relayTo).length);

async function onToggle(region) {
  const target = !region.relayTo;
  if (target && !window.confirm(
    `确认把「${region.label}」的全部播报改投测试区？`
    + '（群消息+DING 都改投功能验证群，带 [测试→' + region.label
    + '] 前缀；真群不再收到；随时可关）')) return;
  emit('update:busy', true);
  try {
    await setRegionRelay({ region_key: region.key, relay: target });
    setFlash('ok', target
      ? `「${region.label}」播报已改投测试区`
      : `「${region.label}」已恢复投真群`);
    await props.reload(['overview']);   // relay_to 挂在组织树域
  } catch (err) {
    setFlash('err', `改投开关失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}
</script>

<template>
  <h2>播报试投</h2>
  <p class="drawer-sub">
    测试区 = 功能验证群 · 改投中 {{ relayedCount }} 个区域
  </p>
  <div class="drawer-sec">
    <h3>区域改投开关</h3>
    <div v-for="region in regions" :key="region.key" class="roster-row">
      <span class="roster-name">{{ region.label }}</span>
      <span class="roster-role">{{ region.relayTo ? '改投中' : '投真群' }}</span>
      <button
        class="switch"
        :class="{ on: !!region.relayTo }"
        :disabled="busy"
        @click="onToggle(region)"
      ><i /></button>
    </div>
    <p class="drawer-line muted">
      开启后该区域群消息与 DING 全部改投功能验证群（标题带
      [测试→原区域] 前缀），网关 30 秒内生效；当日去重幂等键不变，
      不会因改投重发。开关流水见「操作记录」。
    </p>
  </div>
</template>
