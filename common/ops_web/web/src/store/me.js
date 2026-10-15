/**
 * /api/me 响应式单例（无状态库，仅 ref）。
 *
 * 后端响应形状（common/ops_web/app.py · GET /api/me）：
 *   {
 *     status: 'ok', userid, name, is_admin: bool,
 *     capabilities: ['org', 'roster', ...],            // 全局能力
 *     scoped_capabilities: { roster: ['other', ...] }, // 区域级能力（cap → [region]）
 *     regions: [...],                                  // viewer 可操作区域
 *     all_regions: [...],                              // 全量已登记区域
 *   }
 * 未准入 → 401/403（getMe 抛出带 status 的错误，由 router 守卫跳登录）。
 */
import { ref } from 'vue';
import { getMe } from '../api.js';

//: /api/me 的 data；未加载为 null。
export const me = ref(null);

//: 全局 flash（顶栏共享区读取）；形如 { type: 'ok'|'err', text }。
export const flash = ref(null);

let inflight = null;

/** 加载 /api/me；并发的多次调用共享同一在途请求。401/403 会向上抛出。 */
export function loadMe() {
  if (!inflight) {
    inflight = getMe()
      .then((data) => {
        me.value = data || null;
        return me.value;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
}

/** 是否管理员（is_admin）。 */
export function isAdmin() {
  return !!(me.value && me.value.is_admin);
}

/**
 * 能力×范围判定。
 * @param {string} cap 能力键（org/roster/pipelines/audit/calendar）。
 * @param {string|null} region 区域键；为空仅看全局能力。
 */
export function hasCapability(cap, region = null) {
  const info = me.value;
  if (!info) return false;
  const caps = info.capabilities || [];
  if (region == null) return caps.includes(cap);
  if (caps.includes(cap)) return true;
  const scoped = info.scoped_capabilities || {};
  const keys = scoped[cap] || [];
  return keys.includes(region);
}

/** viewer 可操作的区域 key 数组。 */
export function visibleRegions() {
  return (me.value && me.value.regions) || [];
}

/**
 * 持有该能力的**全局或任一区域级**授权（无单区域上下文的菜单/路由守卫用）。
 *
 * 2026-10-15 闭环修复：此前守卫只看全局能力，导致 `org:<region>` 这类
 * 区域级授权在 UI 上根本进不去页面（授权页却允许授出），授权语义不自洽。
 * @param {string} cap 能力键（org/roster/pipelines/audit/calendar）。
 */
export function hasAnyCapability(cap) {
  const info = me.value;
  if (!info) return false;
  if ((info.capabilities || []).includes(cap)) return true;
  return scopedRegions(cap).length > 0;
}

/** 某能力的区域级授权键数组（``scoped_capabilities[cap]``）。 */
export function scopedRegions(cap) {
  const scoped = (me.value && me.value.scoped_capabilities) || {};
  return scoped[cap] || [];
}

/** 设置全局 flash 提示。 */
export function setFlash(type, text) {
  flash.value = { type, text };
}

/** 清除全局 flash。 */
export function clearFlash() {
  flash.value = null;
}
