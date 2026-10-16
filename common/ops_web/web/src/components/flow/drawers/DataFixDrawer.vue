<script setup>
/**
 * 数据修正抽屉（2026-10-16 F2 从 FlowPage.vue 抽出，P5：类型化修数 API）：
 * 第一期=线下事实行人员归属归位；预览→确认落库，全程审计。
 * 执行落库后除重读修正记录外，还 await props.reload() 全量刷新画布
 * （归位会改变区域成员/指标）。
 */
import { onMounted, ref } from 'vue';
import { dataFixRealign, getDataFixAudits } from '../../../api.js';
import { setFlash } from '../../../store/me.js';

const props = defineProps({
  regionNodes: { type: Array, default: () => [] },
  busy: { type: Boolean, default: false },
  reload: { type: Function, required: true },
});
const emit = defineEmits(['close', 'update:busy']);

const fixForm = ref({ person_name: '', from_region: '', to_region: '' });
const fixPreview = ref(null);    // 预览结果（执行前必须预览）
const fixAudits = ref([]);

async function loadFixAudits() {
  try {
    const data = await getDataFixAudits();
    fixAudits.value = (data && data.audits) || [];
  } catch (err) {
    fixAudits.value = [];
  }
}

async function onFixPreview() {
  const f = fixForm.value;
  if (!f.person_name.trim() || !f.from_region || !f.to_region) {
    setFlash('err', '姓名与起止区域必填');
    return;
  }
  emit('update:busy', true);
  try {
    fixPreview.value = await dataFixRealign({
      person_name: f.person_name.trim(),
      from_region: f.from_region,
      to_region: f.to_region,
    });
  } catch (err) {
    fixPreview.value = null;
    setFlash('err', `预览失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

async function onFixApply() {
  const p = fixPreview.value;
  if (!p || !p.row_count) return;
  const ok = window.confirm(
    `确认执行归属归位？\n「${p.person_name}」${p.row_count} 行 `
    + `（${p.date_from}~${p.date_to}，Σ${p.sales_amount}）\n`
    + `${p.from_region} → ${p.to_region}（同事务 UPDATE + 审计）`,
  );
  if (!ok) return;
  emit('update:busy', true);
  try {
    const res = await dataFixRealign({
      person_name: p.person_name,
      from_region: p.from_region,
      to_region: p.to_region,
      apply: true,
    });
    setFlash('ok', `已归位「${res.person_name}」${res.row_count} 行`);
    fixPreview.value = null;
    fixForm.value.person_name = '';
    await loadFixAudits();
    await props.reload();
  } catch (err) {
    setFlash('err', `执行失败：${err.message}`);
  } finally {
    emit('update:busy', false);
  }
}

onMounted(loadFixAudits);
</script>

<template>
  <h2>数据修正</h2>
  <p class="drawer-sub">类型化修数（第一期：线下事实行人员归属归位）；预览→确认落库，全程审计</p>
  <div class="drawer-sec">
    <h3>归属归位</h3>
    <div class="roster-add">
      <input v-model="fixForm.person_name" placeholder="姓名" class="roster-input">
    </div>
    <div class="roster-add">
      <select v-model="fixForm.from_region" class="roster-select">
        <option value="">从区域…</option>
        <option v-for="n in props.regionNodes" :key="n.key" :value="n.key">{{ n.label }}</option>
      </select>
      <span class="drawer-line">→</span>
      <select v-model="fixForm.to_region" class="roster-select">
        <option value="">到区域…</option>
        <option v-for="n in props.regionNodes" :key="n.key" :value="n.key">{{ n.label }}</option>
      </select>
    </div>
    <div class="roster-add">
      <button class="btn mini" :disabled="busy" @click="onFixPreview">预览影响面</button>
      <button
        class="btn mini primary"
        :disabled="busy || !fixPreview || !fixPreview.row_count"
        @click="onFixApply"
      >执行归位</button>
    </div>
    <div v-if="fixPreview" class="fix-preview">
      <template v-if="fixPreview.row_count">
        命中 <b>{{ fixPreview.row_count }}</b> 行 ·
        {{ fixPreview.date_from }} ~ {{ fixPreview.date_to }} ·
        Σ {{ fixPreview.sales_amount }} 元
      </template>
      <span v-else class="muted">零命中（不落审计）</span>
    </div>
  </div>
  <div class="drawer-sec">
    <h3>修正记录（{{ fixAudits.length }}）</h3>
    <div v-for="(a, i) in fixAudits" :key="i" class="rec-row">
      <span class="rec-time">{{ a.created_at }}</span>
      <span class="rec-action">{{ a.action_label }}</span>
      <span class="rec-target">{{ a.fix_type_label }} · {{ a.target }}</span>
      <span class="rec-detail">{{ a.detail }} · {{ a.actor }}</span>
    </div>
    <p v-if="!fixAudits.length" class="drawer-line muted">暂无修正记录</p>
  </div>
</template>
