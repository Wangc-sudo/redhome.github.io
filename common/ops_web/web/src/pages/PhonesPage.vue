<script setup>
/**
 * 号码台账（/phones）——号码 ↔ 实际持卡人 ↔ 平台账号三键映射
 * （2026-10-26 号码台账收尾：「收验证码该找谁」的唯一真源页）。
 *
 * 读面 GET /api/phones（摘要+号码列表）与 /api/phones/bindings（账号绑定）；
 * 写面 claim / save / toggle / binding save / binding toggle，全部留审计
 * （dim_phone_number_audit，「操作记录」页来源=号码台账）。
 * 口径：台账不存任何密码；actual_user=真实持卡人（检索以此为准），
 * account_name=电信开户名（常与持卡人不符，仅参考）。
 */
import { computed, ref } from 'vue';
import {
  getPhones,
  getPhoneBindings,
  claimPhone,
  savePhone,
  togglePhone,
  savePhoneBinding,
  togglePhoneBinding,
} from '../api.js';

const summary = ref(null);
const phones = ref([]);
const bindings = ref([]);
const loading = ref(false);
const error = ref('');
const msg = ref(null);

const keyword = ref('');
const unclaimedOnly = ref(false);
const bindingKeyword = ref('');

//: 号码类型选项（与后端 PHONE_TYPES 对齐）。
const PHONE_TYPES = ['主卡', '副卡', '固话', '未知'];

// -- 号码编辑表单（新增/编辑同形；phone_no 是业务键，编辑时锁死）-----------
const blankForm = () => ({
  phone_no: '', phone_type: '未知', actual_user: '', account_name: '',
  owner: '', dept: '', physical_device: '', status: '', note: '',
});
const form = ref(null);
const formEditing = ref(false);

// -- 认领表单（待认领区块逐行一个输入框）------------------------------------
const claimInputs = ref({});

// -- 绑定新增表单 ------------------------------------------------------------
const bindingForm = ref({
  phone_no: '', channel_category: '', platform_account: '', note: '',
});

const unclaimedPhones = computed(
  () => phones.value.filter((p) => p.needs_claim && p.enabled),
);

function setMsg(type, text) {
  msg.value = { type, text };
}

async function load() {
  loading.value = true;
  error.value = '';
  try {
    const data = await getPhones({
      keyword: keyword.value,
      unclaimed: unclaimedOnly.value ? '1' : '',
    });
    summary.value = data.summary;
    phones.value = data.phones || [];
  } catch (err) {
    error.value = err.message;
    phones.value = [];
  } finally {
    loading.value = false;
  }
}

async function loadBindings() {
  try {
    const data = await getPhoneBindings({ keyword: bindingKeyword.value });
    bindings.value = data.bindings || [];
  } catch (err) {
    setMsg('err', `绑定读取失败：${err.message}`);
    bindings.value = [];
  }
}

load();
loadBindings();

function openCreate() {
  form.value = blankForm();
  formEditing.value = false;
}

function openEdit(phone) {
  form.value = {
    phone_no: phone.phone_no,
    phone_type: phone.phone_type || '未知',
    actual_user: phone.actual_user || '',
    account_name: phone.account_name || '',
    owner: phone.owner || '',
    dept: phone.dept || '',
    physical_device: phone.physical_device || '',
    status: phone.status || '',
    note: phone.note || '',
  };
  formEditing.value = true;
}

async function onSave() {
  if (!form.value.phone_no.trim()) {
    setMsg('err', '号码必填');
    return;
  }
  try {
    await savePhone(form.value);
    setMsg('ok', `${form.value.phone_no} 已保存`);
    form.value = null;
    await load();
  } catch (err) {
    setMsg('err', `保存失败：${err.message}`);
  }
}

async function onToggle(phone) {
  const willEnable = !phone.enabled;
  if (!window.confirm(
    `确认${willEnable ? '启用' : '停用'} ${phone.phone_no}？`
    + (willEnable ? '' : '（停用后不进任何查询面）'),
  )) return;
  try {
    await togglePhone({ phone_no: phone.phone_no, enabled: willEnable });
    setMsg('ok', `${phone.phone_no} 已${willEnable ? '启用' : '停用'}`);
    await load();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}

async function onClaim(phone) {
  const user = (claimInputs.value[phone.phone_no] || '').trim();
  if (!user) {
    setMsg('err', `请先填 ${phone.phone_no} 的持卡人姓名`);
    return;
  }
  try {
    await claimPhone({ phone_no: phone.phone_no, actual_user: user });
    setMsg('ok', `${phone.phone_no} 已认领给 ${user}`);
    claimInputs.value[phone.phone_no] = '';
    await load();
  } catch (err) {
    setMsg('err', `认领失败：${err.message}`);
  }
}

async function onBindingSave() {
  const b = bindingForm.value;
  if (!b.phone_no.trim() || !b.channel_category.trim()
      || !b.platform_account.trim()) {
    setMsg('err', '号码 / 渠道类目 / 平台账号均必填');
    return;
  }
  try {
    await savePhoneBinding(b);
    setMsg('ok', `绑定已保存：${b.platform_account} → ${b.phone_no}`);
    bindingForm.value = {
      phone_no: '', channel_category: '', platform_account: '', note: '',
    };
    await loadBindings();
  } catch (err) {
    setMsg('err', `绑定保存失败：${err.message}`);
  }
}

async function onBindingToggle(binding) {
  const willEnable = !binding.enabled;
  try {
    await togglePhoneBinding({ id: binding.id, enabled: willEnable });
    setMsg('ok', `绑定已${willEnable ? '启用' : '停用'}`);
    await loadBindings();
  } catch (err) {
    setMsg('err', `操作失败：${err.message}`);
  }
}
</script>

<template>
  <div class="page">
    <h1>号码台账</h1>
    <p class="hint">
      号码 ↔ 实际持卡人 ↔ 平台账号三键映射：「XX 店铺账号收验证码，该找谁拿手机」
      查这里。台账不存任何密码；「持卡人」= 实际拿着手机的人（检索以此为准），
      「开户名」= 电信登记名（常与持卡人不符，仅参考）。全部变更留审计
      （操作记录页来源「号码台账」）。
    </p>

    <div v-if="msg" class="flash" :class="msg.type">{{ msg.text }}</div>
    <div v-if="error" class="state error">{{ error }}</div>
    <div v-else-if="loading && !summary" class="state">加载中…</div>

    <template v-if="summary">
      <div class="ph-stats">
        <span class="ph-stat">号码 <b>{{ summary.total }}</b></span>
        <span class="ph-stat warn">待认领 <b>{{ summary.unclaimed }}</b></span>
        <span class="ph-stat muted">失效（停机/空号/关停） <b>{{ summary.dead }}</b></span>
        <span class="ph-stat muted">账号绑定 <b>{{ bindings.length }}</b> 条</span>
      </div>

      <h2>待认领（{{ unclaimedPhones.length }}）</h2>
      <p class="hint">这些号码推断不出实际持卡人；确认后填姓名认领（立即生效）。</p>
      <table>
        <tr>
          <th>号码</th><th>类型</th><th>开户名</th><th>部门</th>
          <th>状态</th><th>认领给</th><th></th>
        </tr>
        <tr v-for="p in unclaimedPhones" :key="p.phone_no">
          <td>{{ p.phone_no }}</td>
          <td>{{ p.phone_type }}</td>
          <td>{{ p.account_name || '—' }}</td>
          <td>{{ p.dept || '—' }}</td>
          <td>{{ p.status || '—' }}</td>
          <td>
            <input
              v-model="claimInputs[p.phone_no]"
              class="ph-claim-input"
              placeholder="持卡人姓名"
            >
          </td>
          <td><button @click="onClaim(p)">认领</button></td>
        </tr>
        <tr v-if="!unclaimedPhones.length">
          <td colspan="7" class="hint">没有待认领的号码</td>
        </tr>
      </table>

      <h2>号码列表</h2>
      <form class="inline" @submit.prevent="load">
        <input
          v-model="keyword"
          placeholder="搜号码 / 持卡人 / 开户名 / 部门 / 设备"
          @change="load"
        >
        <label class="hint">
          <input v-model="unclaimedOnly" type="checkbox" @change="load"> 只看待认领
        </label>
        <button type="submit">查询</button>
        <button type="button" @click="openCreate">新增号码</button>
      </form>

      <form v-if="form" class="ph-form" @submit.prevent="onSave">
        <strong>{{ formEditing ? `编辑 ${form.phone_no}` : '新增号码' }}</strong>
        <input
          v-model="form.phone_no"
          placeholder="号码（必填）"
          :disabled="formEditing"
        >
        <select v-model="form.phone_type">
          <option v-for="t in PHONE_TYPES" :key="t" :value="t">{{ t }}</option>
        </select>
        <input v-model="form.actual_user" placeholder="持卡人（实际使用人）">
        <input v-model="form.account_name" placeholder="开户名（电信登记名）">
        <input v-model="form.owner" placeholder="归属人">
        <input v-model="form.dept" placeholder="使用部门">
        <input v-model="form.physical_device" placeholder="所在物理手机">
        <input v-model="form.status" placeholder="状态（正常/停机/空号…）">
        <input v-model="form.note" placeholder="备注">
        <button type="submit">保存</button>
        <button type="button" @click="form = null">取消</button>
      </form>

      <table>
        <tr>
          <th>号码</th><th>类型</th><th>持卡人</th><th>开户名</th>
          <th>部门</th><th>物理手机</th><th>状态</th><th>备注</th><th></th>
        </tr>
        <tr v-for="p in phones" :key="p.phone_no" :class="{ 'ph-off': !p.enabled }">
          <td>{{ p.phone_no }}</td>
          <td>{{ p.phone_type }}</td>
          <td>
            <b v-if="p.actual_user">{{ p.actual_user }}</b>
            <span v-else-if="p.needs_claim" class="ph-warn">待认领</span>
            <span v-else class="hint">—</span>
          </td>
          <td>{{ p.account_name || '—' }}</td>
          <td>{{ p.dept || '—' }}</td>
          <td>{{ p.physical_device || '—' }}</td>
          <td>{{ p.status || '—' }}</td>
          <td class="ph-note">{{ p.note || '' }}</td>
          <td class="ph-ops">
            <a href="#" @click.prevent="openEdit(p)">编辑</a>
            <a href="#" @click.prevent="onToggle(p)">{{ p.enabled ? '停用' : '启用' }}</a>
          </td>
        </tr>
        <tr v-if="!phones.length">
          <td colspan="9" class="hint">无匹配号码</td>
        </tr>
      </table>

      <h2>平台账号绑定（{{ bindings.length }}）</h2>
      <p class="hint">
        「XX 店铺/平台账号的验证码发到哪个号」——按账号名、类目或号码搜。
      </p>
      <form class="inline" @submit.prevent="loadBindings">
        <input
          v-model="bindingKeyword"
          placeholder="搜账号 / 类目 / 号码，如「习酒窖藏1988」"
          @change="loadBindings"
        >
        <button type="submit">查询</button>
      </form>

      <form class="ph-form" @submit.prevent="onBindingSave">
        <strong>新增绑定</strong>
        <input v-model="bindingForm.platform_account" placeholder="平台/店铺账号（必填）">
        <input v-model="bindingForm.channel_category" placeholder="渠道类目，如 抖音店铺登入主账号">
        <input v-model="bindingForm.phone_no" placeholder="接收验证码手机号（必填）">
        <input v-model="bindingForm.note" placeholder="备注">
        <button type="submit">保存绑定</button>
      </form>

      <table>
        <tr>
          <th>平台/店铺账号</th><th>渠道类目</th><th>接收号码</th>
          <th>持卡人</th><th>物理手机</th><th>号码状态</th><th></th>
        </tr>
        <tr v-for="b in bindings" :key="b.id" :class="{ 'ph-off': !b.enabled }">
          <td>{{ b.platform_account }}</td>
          <td>{{ b.channel_category }}</td>
          <td>{{ b.phone_no }}</td>
          <td>
            <b v-if="b.actual_user">{{ b.actual_user }}</b>
            <span v-else class="ph-warn">未登记</span>
          </td>
          <td>{{ b.physical_device || '—' }}</td>
          <td>{{ b.status || '—' }}</td>
          <td>
            <a href="#" @click.prevent="onBindingToggle(b)">
              {{ b.enabled ? '停用' : '启用' }}
            </a>
          </td>
        </tr>
        <tr v-if="!bindings.length">
          <td colspan="7" class="hint">无匹配绑定</td>
        </tr>
      </table>
    </template>
  </div>
</template>

<style scoped>
.ph-stats {
  display: flex;
  gap: 10px;
  margin: 12px 0 4px;
}
.ph-stat {
  padding: 6px 12px;
  background: #fff;
  border: 1px solid #d7dee8;
  border-radius: 8px;
  font-size: 13px;
}
.ph-stat.warn b { color: #b34a00; }
.ph-stat.muted { color: #8a94a6; }
.ph-form {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  margin: 10px 0;
  padding: 10px 12px;
  background: #f4f7fb;
  border: 1px solid #d7dee8;
  border-radius: 8px;
}
.ph-form input,
.ph-form select {
  padding: 4px 8px;
  border: 1px solid #c4cdda;
  border-radius: 6px;
  font-size: 13px;
}
.ph-claim-input {
  width: 110px;
  padding: 3px 6px;
  border: 1px solid #c4cdda;
  border-radius: 6px;
  font-size: 13px;
}
.ph-ops a {
  margin-right: 8px;
  color: #1f3a5f;
  text-decoration: none;
  font-size: 13px;
}
.ph-note {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ph-off td { color: #a5adba; }
.ph-warn { color: #b34a00; font-size: 12px; }
</style>
