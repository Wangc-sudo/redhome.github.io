<script setup>
/**
 * 人员 × 月目标编辑器（共享组件，spec §4.3）。
 *
 * 「就地操作」抽屉复用 `/roster` 的 `input.person-target` 交互：某区域内
 * 每个名册在册人一个「万元」输入框 → `POST /api/roster/region-targets`
 * （scope + year_month + rows[{person_name, monthly_target}]）。
 *
 * 数据口径与 RosterPage 完全一致：人员列表只由 `dim_report_roster` 生成
 * （`/api/roster` 的 `roster` 行按 scope 过滤），未登记名册的区域无输入框。
 * 空白 = 清除该人当月目标。保存后 emit `saved`，由父级 flash + 刷新。
 */
import { computed, ref, watch } from 'vue';
import { getRoster, saveRegionTargets } from '../api.js';

const props = defineProps({
  /** 区域 key（= scope；qudao 不走此组件）。 */
  scope: { type: String, required: true },
  /** 目标月份 'YYYY-MM'；空串 = 当月。 */
  month: { type: String, default: '' },
  /** 区域中文名（仅用于按钮文案）。 */
  label: { type: String, default: '' },
});
const emit = defineEmits(['saved']);

const loading = ref(true);
const error = ref('');
const saving = ref(false);
const draft = ref({}); //: {person_name: '万元字符串'}
const rows = ref([]); //: 该 scope 下的名册行（已排序）

/** 与 RosterPage.sortRoster 同口径：对象 → 角色 → 姓名 → id。 */
function sortRoster(list) {
  return list.slice().sort((a, b) => {
    const ka = String(a.entity_key);
    const kb = String(b.entity_key);
    if (ka !== kb) return ka < kb ? -1 : 1;
    const ra = a.role === 'deputy' ? 1 : 0;
    const rb = b.role === 'deputy' ? 1 : 0;
    if (ra !== rb) return ra - rb;
    if (a.person_name !== b.person_name)
      return a.person_name < b.person_name ? -1 : 1;
    return (a.id || 0) - (b.id || 0);
  });
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getRoster({ month: props.month || undefined });
    const scoped = sortRoster(
      (data.roster || []).filter((r) => r.scope === props.scope),
    );
    rows.value = scoped;
    const t = (data.targets && data.targets[props.scope]) || {};
    const m = {};
    for (const r of scoped) {
      const v = t[r.person_name];
      m[r.person_name] = v == null ? '' : String(Math.round(v));
    }
    draft.value = m;
  } catch (e) {
    error.value = (e && e.message) || '加载失败';
    rows.value = [];
    draft.value = {};
  } finally {
    loading.value = false;
  }
}

watch(() => [props.scope, props.month], load, { immediate: true });

const saveLabel = computed(
  () => `保存${props.label || props.scope}${props.month ? props.month : ''}月目标`,
);

async function save() {
  const rowsPayload = [];
  for (const [name, val] of Object.entries(draft.value)) {
    const raw = String(val == null ? '' : val).trim();
    rowsPayload.push({
      person_name: name,
      monthly_target: raw === '' ? null : Number(raw),
    });
  }
  saving.value = true;
  error.value = '';
  try {
    const res = await saveRegionTargets({
      scope: props.scope,
      year_month: props.month || undefined,
      rows: rowsPayload,
    });
    emit('saved', res || {});
  } catch (e) {
    error.value = (e && e.message) || '保存失败';
  } finally {
    saving.value = false;
  }
}
</script>

<template>
  <div class="target-editor">
    <div v-if="loading" class="state">加载中…</div>
    <template v-else>
      <p v-if="!rows.length" class="hint">
        该区域暂无「填报名册」登记，无法录入个人月目标——请先到
        <router-link :to="{ path: '/roster', query: { region: scope } }">填报名册</router-link>
        登记填报名册人。
      </p>
      <template v-else>
        <table>
          <thead>
            <tr>
              <th>对象</th>
              <th>填报人</th>
              <th>月目标（万元）</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="r in rows" :key="r.id">
              <td>{{ r.entity_key }}</td>
              <td>
                {{ r.person_name }}
                <span v-if="r.role === 'deputy'" class="hint">（代填报人）</span>
              </td>
              <td>
                <input
                  v-model="draft[r.person_name]"
                  class="person-target"
                  placeholder="目标（万元）"
                  :data-scope="scope"
                  :data-name="r.person_name"
                />
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="error" class="hint err">{{ error }}</p>
        <p class="hint">
          单位：万元；空白 = 清除该人当月目标。保存即生效——机器人报数快照同步读取，逐条落审计。
        </p>
        <button class="btn primary" :disabled="saving" @click="save">
          {{ saving ? '保存中…' : saveLabel }}
        </button>
      </template>
    </template>
  </div>
</template>

<style scoped>
input.person-target {
  width: 96px;
}
p.err {
  color: #b91c1c;
}
</style>
