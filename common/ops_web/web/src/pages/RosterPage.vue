<script setup>
/**
 * 填报名册（旧 SSR /roster 迁移，C 阶段）。
 *
 * 数据源：GET /api/roster（名册 + 区域个人月目标 + 变更流水）与
 * GET /api/roster/qudao（渠道门店一体读面，替代 SSR 的 qudao 段）。
 * 写：/api/roster/add · /toggle · /delete · /region-targets。
 * 口径与文案照旧 SSR 页（app.py `_qudao_section` / /roster 分支）逐条对齐。
 */
import { computed, nextTick, onMounted, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import {
  addRosterEntry,
  deleteRosterEntry,
  getRoster,
  getRosterQudao,
  saveRegionTargets,
  toggleRosterEntry,
} from '../api.js';
import { hasCapability, setFlash } from '../store/me.js';

const route = useRoute();
const router = useRouter();

const data = ref(null);
const qudaoData = ref(null);
const loading = ref(true);
const error = ref('');
//: 区域个人月目标草稿：{scope: {person_name: 字符串值（万元）}}。
const targetDraft = ref({});
const monthInput = ref('');

//: 中文标签（照 app.py _SCOPE_LABEL / _ACTION_LABEL / _ENTITY_LABEL）。
const SCOPE_LABEL = {
  qudao: '渠道门店', hangzhou: '杭州', shaoxing: '绍兴', junpin: '君品雅院',
  vanke: '万科&大莲花&团购', offline_all: '线下整体', dining: '餐饮/部门',
  hq: '总部', other: '其他',
};
const ACTION_LABEL = {
  add: '新增', enable: '启用', disable: '停用', delete: '删除',
  seed: '种子导入', publish: '发布快照', update: '修正', schedule: '改定时',
  carry: '结转', upsert: '改判', reset: '恢复规则', import: '法定导入',
  reset_month: '月度恢复', target: '月目标', grant: '授权', revoke: '收回',
};
const ENTITY_OPTIONS = [
  { value: 'store', label: '门店' },
  { value: 'person', label: '人员' },
  { value: 'dept', label: '部门' },
];
//: 渠道排序序（照 back 端 CHANNELS）。
const CHANNELS = ['即时零售', '拼多多', '京东', '天猫', '直播', '私域', '猫超'];

const monthQuery = computed(() =>
  typeof route.query.month === 'string' ? route.query.month : '',
);
const regionQuery = computed(() =>
  typeof route.query.region === 'string' ? route.query.region : '',
);

function fmtMonth(d) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}
function shiftMonth(ym, delta) {
  const [y, m] = ym.split('-').map(Number);
  return fmtMonth(new Date(y, m - 1 + delta, 1));
}
function fmtTime(v) {
  if (v == null || v === '') return '';
  return String(v).replace('T', ' ').split('.')[0];
}

const currentMonth = fmtMonth(new Date());
const regions = computed(() => (data.value && data.value.regions) || []);
const selectedMonth = computed(
  () => (data.value && data.value.month) || monthQuery.value || currentMonth,
);
const monthLabel = computed(() => selectedMonth.value);
const monthLinks = computed(() => [
  { value: currentMonth, tag: '本月' },
  { value: shiftMonth(currentMonth, -1), tag: '上月' },
  { value: shiftMonth(currentMonth, 1), tag: '下月' },
]);
const editableScopes = computed(() =>
  regions.value.filter((r) => hasCapability('roster', r.region_key)),
);

function canEdit(region) {
  return hasCapability('roster', region);
}
function queryWith(month) {
  const q = { ...route.query };
  if (month) q.month = month;
  else delete q.month;
  return { path: '/roster', query: q };
}
function goMonth(v) {
  const ok = /^20\d{2}-(0[1-9]|1[0-2])$/.test(v);
  router.replace(queryWith(ok ? v : ''));
}
function scopeLabel(scope) {
  return SCOPE_LABEL[scope] || scope || '';
}
function actionLabel(a) {
  return ACTION_LABEL[a] || a || '';
}
function aliasText(row) {
  if (!row) return '';
  let arr = row.aliases;
  if (typeof arr === 'string') {
    try {
      arr = JSON.parse(arr);
    } catch (err) {
      arr = [];
    }
  }
  return Array.isArray(arr) && arr.length ? arr.join('、') : '';
}

async function loadAll() {
  loading.value = true;
  error.value = '';
  try {
    const [r, q] = await Promise.all([
      getRoster({ month: monthQuery.value || undefined }),
      getRosterQudao().catch(() => null),
    ]);
    data.value = r;
    qudaoData.value = q;
    initDrafts();
    monthInput.value = r.month || currentMonth;
  } catch (e) {
    error.value = (e && e.message) || '加载失败';
  } finally {
    loading.value = false;
    if (regionQuery.value) {
      await nextTick();
      const el = document.getElementById('sec-' + regionQuery.value);
      if (el) el.scrollIntoView({ block: 'start' });
    }
  }
}

function initDrafts() {
  const d = data.value;
  if (!d) return;
  const drafts = {};
  for (const reg of d.regions || []) {
    if (reg.region_key === 'qudao') continue;
    const t = (d.targets && d.targets[reg.region_key]) || {};
    const m = {};
    for (const row of d.roster || []) {
      if (row.scope !== reg.region_key) continue;
      const v = t[row.person_name];
      m[row.person_name] = v == null ? '' : String(Math.round(v));
    }
    drafts[reg.region_key] = m;
  }
  targetDraft.value = drafts;
}

function sortRoster(rows) {
  return rows.slice().sort((a, b) => {
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
function decorate(rows) {
  const spans = {};
  for (const r of rows) {
    const k = String(r.entity_key);
    spans[k] = (spans[k] || 0) + 1;
  }
  const seen = new Set();
  return rows.map((r) => {
    const k = String(r.entity_key);
    const first = !seen.has(k);
    if (first) seen.add(k);
    return { row: r, entitySpan: spans[k], firstEntity: first };
  });
}
const personByScope = computed(() => {
  const d = data.value;
  const map = {};
  if (!d) return map;
  for (const reg of d.regions || []) {
    if (reg.region_key === 'qudao') continue;
    map[reg.region_key] = decorate(
      sortRoster((d.roster || []).filter((r) => r.scope === reg.region_key)),
    );
  }
  return map;
});

function channelRank(ch) {
  const idx = CHANNELS.indexOf(ch);
  return idx >= 0 ? [0, idx] : [1, ch || ''];
}
const qudaoRows = computed(() => {
  const stores = (qudaoData.value && qudaoData.value.stores) || [];
  const ordered = stores.slice().sort((a, b) => {
    const ca = channelRank(a.channel);
    const cb = channelRank(b.channel);
    if (ca[0] !== cb[0]) return ca[0] - cb[0];
    if (ca[0] === 0) {
      if (ca[1] !== cb[1]) return ca[1] - cb[1];
    } else {
      const x = String(ca[1]);
      const y = String(cb[1]);
      if (x !== y) return x < y ? -1 : 1;
    }
    const sa = a.store_no == null ? 1e6 : a.store_no;
    const sb = b.store_no == null ? 1e6 : b.store_no;
    if (sa !== sb) return sa - sb;
    return a.store < b.store ? -1 : 1;
  });
  const flat = [];
  for (const st of ordered) {
    const links = st.links || [];
    const ownerLinks = links.filter((l) => l.role !== 'deputy');
    const deputyLinks = links.filter((l) => l.role === 'deputy');
    const span = ownerLinks.length || 1;
    if (!ownerLinks.length) {
      flat.push({ store: st, link: null, span, deputyLinks, firstOfStore: true });
    } else {
      ownerLinks.forEach((l, i) =>
        flat.push({ store: st, link: l, span, deputyLinks, firstOfStore: i === 0 }),
      );
    }
  }
  // 默认按渠道聚合：连续同渠道行合并「渠道/地区」格（对应 SSR sort=channel）。
  let i = 0;
  while (i < flat.length) {
    let j = i;
    const ch = flat[i].store.channel;
    while (j < flat.length && flat[j].store.channel === ch) j += 1;
    flat[i].channelSpan = j - i;
    for (let k = i + 1; k < j; k += 1) flat[k].channelSpan = 0;
    i = j;
  }
  return flat;
});
const qudaoTotal = computed(() => {
  let t = 0;
  for (const st of (qudaoData.value && qudaoData.value.stores) || []) {
    if (st.target != null) t += Number(st.target);
  }
  return Math.round(t);
});

async function onToggle(row, ev) {
  const enabled = ev.target.value === '1';
  try {
    await toggleRosterEntry({ id: row.id, enabled });
    setFlash('ok', enabled ? '已启用' : '已停用');
  } catch (e) {
    setFlash('err', (e && e.message) || '操作失败');
  }
  await loadAll();
}
async function onDelete(row) {
  if (
    !window.confirm(
      '确认删除该名册记录？（审计会留存，日常建议用「停用」）',
    )
  )
    return;
  try {
    await deleteRosterEntry({ id: row.id });
    setFlash('ok', '已删除（审计留存）');
  } catch (e) {
    setFlash('err', (e && e.message) || '删除失败');
  }
  await loadAll();
}
async function saveTargets(scope) {
  const m = targetDraft.value[scope] || {};
  const rows = [];
  for (const [name, val] of Object.entries(m)) {
    const raw = String(val == null ? '' : val).trim();
    rows.push({ person_name: name, monthly_target: raw === '' ? null : Number(raw) });
  }
  try {
    await saveRegionTargets({ scope, year_month: selectedMonth.value, rows });
    setFlash('ok', '月目标已保存');
    await loadAll();
  } catch (e) {
    setFlash('err', (e && e.message) || '保存失败');
  }
}

//: 新增表单（照旧 SSR addRoster 载荷字段）。
const addForm = ref({
  scope: '',
  entity_type: 'store',
  entity_key: '',
  person_name: '',
  role: 'owner',
  aliases: '',
  note: '',
});
watch(
  regions,
  () => {
    if (!addForm.value.scope && regions.value.length)
      addForm.value.scope = regions.value[0].region_key;
  },
  { immediate: true },
);
async function submitAdd() {
  const f = addForm.value;
  try {
    await addRosterEntry({
      scope: f.scope,
      entity_type: f.entity_type,
      entity_key: f.entity_key.trim(),
      person_name: f.person_name.trim(),
      aliases: f.aliases.split(/[\s,，、]+/).filter(Boolean),
      note: f.note.trim(),
      role: f.role,
    });
    setFlash('ok', '名册已新增');
    addForm.value.entity_key = '';
    addForm.value.person_name = '';
    addForm.value.aliases = '';
    addForm.value.note = '';
    addForm.value.role = 'owner';
    await loadAll();
  } catch (e) {
    setFlash('err', (e && e.message) || '新增失败');
  }
}

watch(() => route.query, () => loadAll());
watch(selectedMonth, (v) => {
  monthInput.value = v;
});

onMounted(loadAll);
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>填报名册（谁可以填哪些店/区域）</h1>
        <p class="note">
          名册定资格、覆盖定单条例外、授权定系统访问（与催办无关）——完整口径见
          <router-link to="/recipients">收件人总览</router-link>。
        </p>
      </div>
    </div>

    <p class="month-nav">
      区域个人月目标的目标月份：
      <template v-for="(m, idx) in monthLinks" :key="m.value">
        <span v-if="idx > 0" class="sep"> ｜ </span>
        <strong v-if="m.value === selectedMonth">{{ m.value }}（{{ m.tag }}）</strong>
        <router-link v-else :to="queryWith(m.value)">
          {{ m.value }}（{{ m.tag }}）
        </router-link>
      </template>
      <form class="month-form" @submit.prevent="goMonth(monthInput)">
        <input v-model="monthInput" type="month" required />
        <button class="btn" type="submit">切换</button>
      </form>
      <span class="hint">
        （每月 1 日 06:00 上月目标自动结转到当月，人工已录入的不覆盖；渠道门店目标不带月份，常年有效）
      </span>
    </p>

    <div v-if="loading" class="state">加载中…</div>
    <div v-else-if="error" class="state error">{{ error }}</div>
    <template v-else-if="data">
      <template v-for="reg in regions" :key="reg.region_key">
        <!-- 渠道门店段（qudao）：目标 + 负责人一体（数据取 /api/roster/qudao） -->
        <section
          v-if="reg.region_key === 'qudao'"
          id="sec-qudao"
          class="section"
          :class="{ 'section-active': regionQuery === 'qudao' }"
        >
          <h2>
            渠道门店
            <span class="hint">（qudao，目标 + 负责人一体管理；默认按渠道聚合）</span>
          </h2>
          <p v-if="!canEdit('qudao')" class="hint">无该区域编辑授权（只读）。</p>
          <table>
            <thead>
              <tr>
                <th>渠道/地区</th>
                <th>店铺/对象</th>
                <th>店号</th>
                <th>门店目标（元）</th>
                <th>负责人</th>
                <th>代填报人</th>
                <th>状态</th>
                <th>备注</th>
                <th>更新</th>
                <th>操作</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(f, i) in qudaoRows" :key="i">
                <td v-if="f.channelSpan" :rowspan="f.channelSpan > 1 ? f.channelSpan : undefined">
                  {{ f.store.channel || '—' }}
                </td>
                <td v-if="f.firstOfStore" :rowspan="f.span > 1 ? f.span : undefined">
                  {{ f.store.store }}
                </td>
                <td v-if="f.firstOfStore" :rowspan="f.span > 1 ? f.span : undefined">
                  {{ f.store.store_no == null ? '—' : f.store.store_no }}
                </td>
                <td v-if="f.firstOfStore" :rowspan="f.span > 1 ? f.span : undefined">
                  {{ f.store.target == null ? '' : Math.round(f.store.target) }}
                </td>
                <td>
                  <template v-if="f.link">
                    {{ f.link.person_name }}<span v-if="aliasText(f.link)" class="hint">（{{ aliasText(f.link) }}）</span>
                  </template>
                  <span v-else class="hint">无负责人在册</span>
                </td>
                <td v-if="f.firstOfStore" :rowspan="f.span > 1 ? f.span : undefined">
                  <template v-if="f.deputyLinks.length">
                    <div v-for="d in f.deputyLinks" :key="d.id">
                      {{ d.person_name }}<span v-if="aliasText(d)" class="hint">（{{ aliasText(d) }}）</span>
                    </div>
                  </template>
                  <span v-else class="hint">—</span>
                </td>
                <template v-if="f.link">
                  <td>
                    <select
                      :value="f.link.enabled ? '1' : '0'"
                      :disabled="!canEdit('qudao')"
                      @change="onToggle(f.link, $event)"
                    >
                      <option value="1">启用</option>
                      <option value="0">停用</option>
                    </select>
                  </td>
                  <td>{{ f.link.note || '' }}</td>
                  <td><span class="hint">{{ f.link.updated_at || '—' }}</span></td>
                  <td>
                    <button class="btn" :disabled="!canEdit('qudao')" @click="onDelete(f.link)">
                      删除
                    </button>
                  </td>
                </template>
                <template v-else>
                  <td></td>
                  <td></td>
                  <td></td>
                  <td></td>
                </template>
              </tr>
              <tr v-if="!qudaoRows.length">
                <td class="hint" colspan="10">暂无记录</td>
              </tr>
            </tbody>
          </table>
          <p>
            合计：<strong>{{ qudaoTotal }}</strong> 元
            <span class="hint">
              （单位：元，与区域个人月目标的「万元」口径不同；门店目标随月度快照流程维护）。
            </span>
          </p>
        </section>

        <!-- 区域/部门段：区域个人月目标（万元）+ 名册链接 -->
        <section
          v-else
          :id="'sec-' + reg.region_key"
          class="section"
          :class="{ 'section-active': regionQuery === reg.region_key }"
        >
          <h2>
            {{ reg.label }}
            <span class="hint">（{{ personByScope[reg.region_key].length }} 条）</span>
          </h2>
          <p v-if="!canEdit(reg.region_key)" class="hint">无该区域编辑授权（只读）。</p>
          <p v-if="!personByScope[reg.region_key].length" class="hint">
            暂无记录——该范围当前不拦截（没登记就不限制填报，维持现状）。
          </p>
          <template v-else>
            <table>
              <thead>
                <tr>
                  <th>渠道/地区</th>
                  <th>店铺/对象</th>
                  <th>月目标（万元）</th>
                  <th>负责人</th>
                  <th>代填报人</th>
                  <th>状态</th>
                  <th>备注</th>
                  <th>更新</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="(it, i) in personByScope[reg.region_key]" :key="it.row.id">
                  <td
                    v-if="i === 0"
                    :rowspan="personByScope[reg.region_key].length > 1 ? personByScope[reg.region_key].length : undefined"
                  >
                    {{ reg.label }}
                  </td>
                  <td v-if="it.firstEntity" :rowspan="it.entitySpan > 1 ? it.entitySpan : undefined">
                    {{ it.row.entity_key }}
                  </td>
                  <td>
                    <input
                      v-model="targetDraft[reg.region_key][it.row.person_name]"
                      class="person-target"
                      placeholder="目标（万元）"
                      :data-scope="reg.region_key"
                      :data-name="it.row.person_name"
                      :disabled="!canEdit(reg.region_key)"
                    />
                  </td>
                  <td>
                    <template v-if="it.row.role !== 'deputy'">
                      {{ it.row.person_name }}<span v-if="aliasText(it.row)" class="hint">（{{ aliasText(it.row) }}）</span>
                    </template>
                  </td>
                  <td>
                    <template v-if="it.row.role === 'deputy'">
                      {{ it.row.person_name }}<span v-if="aliasText(it.row)" class="hint">（{{ aliasText(it.row) }}）</span>
                    </template>
                  </td>
                  <td>
                    <select
                      :value="it.row.enabled ? '1' : '0'"
                      :disabled="!canEdit(reg.region_key)"
                      @change="onToggle(it.row, $event)"
                    >
                      <option value="1">启用</option>
                      <option value="0">停用</option>
                    </select>
                  </td>
                  <td>{{ it.row.note || '' }}</td>
                  <td><span class="hint">{{ fmtTime(it.row.updated_at) || '—' }}</span></td>
                  <td>
                    <button class="btn" :disabled="!canEdit(reg.region_key)" @click="onDelete(it.row)">
                      删除
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
            <p v-if="canEdit(reg.region_key)">
              <button class="btn" @click="saveTargets(reg.region_key)">
                保存{{ reg.label }}{{ monthLabel }}月目标
              </button>
              <span class="hint">
                （单位：万元，空白 = 清除该人当月目标；保存即生效——机器人报数快照同步读取，逐条落审计）
              </span>
            </p>
          </template>
        </section>
      </template>

      <h2>新增名册记录</h2>
      <form v-if="editableScopes.length" class="inline" @submit.prevent="submitAdd">
        <select v-model="addForm.scope">
          <option v-for="s in editableScopes" :key="s.region_key" :value="s.region_key">
            {{ s.label }}
          </option>
        </select>
        <select v-model="addForm.entity_type">
          <option v-for="e in ENTITY_OPTIONS" :key="e.value" :value="e.value">{{ e.label }}</option>
        </select>
        <input v-model="addForm.entity_key" placeholder="对象（店名/部门名/区域）" required />
        <input v-model="addForm.person_name" placeholder="填报人姓名" required />
        <select v-model="addForm.role">
          <option value="owner">负责人</option>
          <option value="deputy">代填报人</option>
        </select>
        <input v-model="addForm.aliases" placeholder="别名，逗号分隔（可空）" />
        <input v-model="addForm.note" placeholder="备注（可空）" />
        <button class="btn" type="submit">新增</button>
      </form>
      <p class="hint">
        渠道门店（qudao）：管理「谁可以填哪些店」，渠道机器人实时生效；日报区域（杭州/绍兴等）：
        该范围一旦有人名记录即启用白名单（只准在册人员填报），无记录则不拦截；餐饮/部门先登记，
        机器人后续接入。「删除」会留审计，日常调整建议用「停用」。
        <strong>人名一律用通讯录本名（不用花名/昵称）</strong>；别名仅用于群昵称与本名不一致的匹配容错。
        角色：负责人占业绩归属，代填报人仅可填报不占业绩；改角色 = 用新角色重新「新增」同人同对象（覆盖生效）。
        本页新增/删除/状态修改需「名册管理」授权（成员与授权页单人授权：scope = roster）。
      </p>

      <h2>名册变更流水（最近 20 条）</h2>
      <table>
        <thead>
          <tr>
            <th>时间</th>
            <th>操作人</th>
            <th>动作</th>
            <th>范围</th>
            <th>对象</th>
            <th>填报人</th>
            <th>详情</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(a, i) in (data.audits || [])" :key="i">
            <td>{{ fmtTime(a.created_at) }}</td>
            <td>{{ a.actor }}</td>
            <td>{{ actionLabel(a.action) }}</td>
            <td>{{ scopeLabel(a.scope) }}</td>
            <td>{{ a.entity_key }}</td>
            <td>{{ a.person_name }}</td>
            <td>{{ a.detail || '' }}</td>
          </tr>
          <tr v-if="!(data.audits || []).length">
            <td class="hint" colspan="7">暂无变更</td>
          </tr>
        </tbody>
      </table>
    </template>
  </div>
</template>

<style scoped>
.month-nav {
  margin: 12px 0 4px;
}
.month-form {
  display: inline;
  margin-left: 8px;
}
.section {
  margin-top: 8px;
}
.section-active {
  outline: 2px solid #bfdbfe;
  border-radius: 8px;
  padding: 8px;
}
h2 .hint {
  font-weight: normal;
  margin-left: 6px;
}
input.person-target {
  width: 96px;
}
</style>
