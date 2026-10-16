<script setup>
/**
 * 授权现状（/grants，别名 /audit）——G4 区域分权自助写面（2026-10-14）。
 *
 * 读面：
 *   GET /api/grants → {grants:[{user_id,grant_type,grant_type_label,grant_key,note,created_at}]}
 *   GET /api/audit  → {rows:[{created_at,actor,action,action_label,user_id,
 *                            grant_type,grant_type_label,grant_key,note}]}
 * 写面（仅 admin，deny-by-default）：
 *   POST /api/grants         → 新增一条授权（grant + audit 事务两写）
 *   POST /api/grants/delete  → 删除一条授权
 *
 * grant_key 语义（bi_authz._SCOPE_KEY_RE 已放行 region 级形态）：
 *   scope  → `<capability>`（全局）或 `<capability>:<region>`（限该区域，如 roster:other）
 *   region → `<region>`（看板区域数据行级裁剪）：bi-web 把该键集合注入带
 *            region 维度的卡片查询（参数化 region IN），键须与事实表
 *            fact_daily_report_offline.region 同值（hangzhou/shaoxing/…）；
 *            事实表无同键时是 fail-closed 空数据，绝不放大可见面。
 *   admin  → `-`（占位，admin 无对象维度）
 *
 * ?user= 供 /records 深链而来，用于过滤/高亮某账号。
 * 区域清单源 `me.all_regions`（dim_org_region 真源，禁硬编码）。
 */
import { computed, ref, watch } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { getGrants, getAudit, addGrant, deleteGrant } from '../api.js';
import { me } from '../store/me.js';

const route = useRoute();
const router = useRouter();

//: 能力枚举（与后端 CAPABILITY_* 对齐）；`region` 说明见 §注释。
const CAPABILITIES = [
  { key: 'roster', label: '名册与目标' },
  { key: 'org', label: '组织编排（区域级暂按全局生效）' },
  { key: 'pipelines', label: '定时任务' },
  { key: 'audit', label: '操作记录' },
  { key: 'calendar', label: '播报日历' },
];

const grants = ref([]);
const audit = ref([]);
const loading = ref(false);
const error = ref('');
//: B5「暂无行级数据」徽标真源（/api/grants 附带；probe 失败为空 = 不标注）。
const emptyRowRegions = ref([]);

//: 写面表单状态。
const form = ref({ user_id: '', grant_type: 'scope', note: '' });
const scopeCap = ref('roster');
const scopeRegion = ref(''); //: '' = 全局；否则为区域 key → `<cap>:<region>`
const regionKey = ref('');
const submitting = ref(false);
const flash = ref(null); //: { type:'ok'|'err', text }

const allRegions = computed(() => (me.value && me.value.all_regions) || []);
const hasRegions = computed(() => allRegions.value.length > 0);
const emptyRowSet = computed(() => new Set(emptyRowRegions.value));

/** 区域 option 文案：无行级数据的键加标注（授权后看板为空，属预期）。 */
function regionOptionLabel(key) {
  return emptyRowSet.value.has(key) ? `${key}（暂无行级数据）` : key;
}

const activeUser = computed(() => route.query.user || '');

/** 构造 grant_key：admin→'-'；region→<region>；scope→<cap> 或 <cap>:<region>。 */
const grantKey = computed(() => {
  if (form.value.grant_type === 'admin') return '-';
  if (form.value.grant_type === 'region') return regionKey.value;
  const cap = scopeCap.value;
  return scopeRegion.value ? `${cap}:${scopeRegion.value}` : cap;
});

const visibleGrants = computed(() => {
  const user = activeUser.value;
  return user ? grants.value.filter((g) => g.user_id === user) : grants.value;
});
const visibleAudit = computed(() => {
  const user = activeUser.value;
  return user ? audit.value.filter((r) => r.user_id === user) : audit.value;
});

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const [grantsData, auditData] = await Promise.all([getGrants(), getAudit()]);
    grants.value = (grantsData && grantsData.grants) || [];
    emptyRowRegions.value = (grantsData && grantsData.empty_row_regions) || [];
    audit.value = (auditData && auditData.rows) || [];
  } catch (err) {
    error.value = err.message;
    grants.value = [];
    emptyRowRegions.value = [];
    audit.value = [];
  } finally {
    loading.value = false;
  }
}

async function submitGrant() {
  flash.value = null;
  if (!form.value.user_id.trim()) {
    flash.value = { type: 'err', text: '请填写账号（钉钉 userid）' };
    return;
  }
  if (!grantKey.value) {
    flash.value = { type: 'err', text: '请选择授权对象' };
    return;
  }
  submitting.value = true;
  try {
    await addGrant({
      user_id: form.value.user_id.trim(),
      grant_type: form.value.grant_type,
      grant_key: grantKey.value,
      note: form.value.note,
    });
    flash.value = { type: 'ok', text: `已新增授权：${grantKey.value}` };
    form.value.user_id = '';
    form.value.note = '';
    await load();
  } catch (err) {
    flash.value = {
      type: 'err',
      text: err.status === 400 ? '字段非法（类型 / 对象不匹配）' : err.message,
    };
  } finally {
    submitting.value = false;
  }
}

async function removeGrant(row) {
  flash.value = null;
  const target = `${row.user_id} · ${row.grant_type_label} · ${row.grant_key}`;
  if (!window.confirm(`确认删除该授权？\n${target}`)) return;
  try {
    await deleteGrant({
      user_id: row.user_id,
      grant_type: row.grant_type,
      grant_key: row.grant_key,
    });
    flash.value = { type: 'ok', text: '已删除该授权' };
    await load();
  } catch (err) {
    flash.value = { type: 'err', text: err.message };
  }
}

function clearUser() {
  router.replace({ query: {} });
}

watch(() => route.query.user, load, { immediate: true });
</script>

<template>
  <div class="page">
    <div class="head">
      <div class="head-main">
        <h1>授权管理</h1>
        <p class="note">
          能力 × 范围自助授权（区域清单来自组织注册表 <code>dim_org_region</code>）。
          区域级键形如 <code>roster:other</code>，仅限该区域；未授权一律 fail-closed。
          看板区域数据（region）按钉钉日报事实表的区域口径逐行裁剪：未授权/键与事实表
          不一致时该账号在区域卡片上看到空数据，<strong>不会</strong>因此看到其他区域。
        </p>
      </div>
    </div>

    <form class="inline grant-form" @submit.prevent="submitGrant">
      <input
        v-model="form.user_id"
        class="search"
        placeholder="账号（钉钉 userid）"
      />
      <select v-model="form.grant_type">
        <option value="scope">能力（scope）</option>
        <option value="region">看板区域数据（region）</option>
        <option value="admin">管理员（admin）</option>
      </select>

      <template v-if="form.grant_type === 'admin'">
        <span class="hint">对象固定为 “-”</span>
      </template>
      <template v-else-if="form.grant_type === 'region'">
        <select v-if="hasRegions" v-model="regionKey">
          <option value="">— 选择区域 —</option>
          <option v-for="r in allRegions" :key="r" :value="r">{{ regionOptionLabel(r) }}</option>
        </select>
        <input v-else v-model="regionKey" placeholder="区域 key" />
        <span class="hint">
          该键须与看板事实表的区域值一致（如 <code>hangzhou</code> /
          <code>shaoxing</code>）才会裁剪出对应区域的数据；<code>offline_all</code>
          为「线下整体」聚合授权键（视同放开杭/绍/省外三区并集）；标注
          「暂无行级数据」的键可授但授后看板为空（属预期）。
        </span>
      </template>
      <template v-else>
        <select v-model="scopeCap">
          <option v-for="c in CAPABILITIES" :key="c.key" :value="c.key">
            {{ c.label }}
          </option>
        </select>
        <select v-if="hasRegions" v-model="scopeRegion">
          <option value="">全局</option>
          <option v-for="r in allRegions" :key="r" :value="r">限区域：{{ r }}</option>
        </select>
        <input v-else v-model="scopeRegion" placeholder="区域 key（留空=全局）" />
      </template>

      <input v-model="form.note" class="search" placeholder="备注（可选）" />
      <button type="submit" class="btn primary" :disabled="submitting">
        {{ submitting ? '提交中…' : '新增授权' }}
      </button>
      <span class="hint">将写入：<code>{{ grantKey || '—' }}</code></span>
    </form>

    <div v-if="flash" class="flash" :class="flash.type">{{ flash.text }}</div>

    <p v-if="activeUser" class="user-filter">
      仅显示账号 <strong>{{ activeUser }}</strong> 的授权
      <button type="button" class="btn mini" @click="clearUser">清除</button>
    </p>

    <div v-if="error" class="state error">{{ error }}</div>
    <div v-else-if="loading" class="state">加载中…</div>

    <template v-else>
      <h2>授权现状</h2>
      <table>
        <tr>
          <th>账号</th><th>类型</th><th>对象</th><th>备注</th><th>更新时间</th><th>操作</th>
        </tr>
        <tr v-for="(row, i) in visibleGrants" :key="i">
          <td>{{ row.user_id }}</td>
          <td>{{ row.grant_type_label }}</td>
          <td>{{ row.grant_key }}</td>
          <td>{{ row.note }}</td>
          <td>{{ row.created_at }}</td>
          <td>
            <button type="button" class="btn mini danger" @click="removeGrant(row)">
              删除
            </button>
          </td>
        </tr>
        <tr v-if="!visibleGrants.length">
          <td colspan="6" class="hint">暂无授权</td>
        </tr>
      </table>

      <h2>授权审计</h2>
      <table>
        <tr>
          <th>时间</th><th>操作人</th><th>动作</th><th>账号</th>
          <th>类型</th><th>对象</th><th>备注</th>
        </tr>
        <tr v-for="(row, i) in visibleAudit" :key="i">
          <td>{{ row.created_at }}</td>
          <td>{{ row.actor }}</td>
          <td>{{ row.action_label }}</td>
          <td>{{ row.user_id }}</td>
          <td>{{ row.grant_type_label }}</td>
          <td>{{ row.grant_key }}</td>
          <td>{{ row.note }}</td>
        </tr>
        <tr v-if="!visibleAudit.length">
          <td colspan="7" class="hint">暂无审计记录</td>
        </tr>
      </table>
    </template>
  </div>
</template>

<style scoped>
.grant-form {
  align-items: center;
}
.grant-form code {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
}
.user-filter {
  margin: 10px 0;
  padding: 6px 10px;
  border-radius: 6px;
  background: #eef4ff;
  font-size: 13px;
  color: #1f3a5f;
}
.user-filter .btn {
  margin-left: 8px;
}
</style>
