/**
 * 画布节点坐标持久化（2026-10-17 F3 从 FlowPage.vue 抽出 + 补版本迁移）：
 * localStorage 键 ops-web.flow.node-pos.v3；旧版 v1/v2 键在首次装载时迁移
 * 并入 v3 后删除（此前是手工 bump 直接弃置旧坐标）。
 */
import { ref } from 'vue';

const POS_KEY = 'ops-web.flow.node-pos.v3';
const LEGACY_KEYS = ['ops-web.flow.node-pos.v2', 'ops-web.flow.node-pos.v1'];

function readKey(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || '{}') || {};
  } catch (err) {
    return {};
  }
}

export function useNodePositions() {
  const nodePos = ref({});

  function loadPos() {
    const current = readKey(POS_KEY);
    const migrated = {};
    for (const key of LEGACY_KEYS) {
      const legacy = readKey(key);
      // 只补缺不覆盖：v3 已有坐标优先（迁移幂等）
      for (const [id, pos] of Object.entries(legacy)) {
        if (!(id in current) && !(id in migrated)
            && pos && Number.isFinite(pos.x) && Number.isFinite(pos.y)) {
          migrated[id] = pos;
        }
      }
    }
    nodePos.value = { ...migrated, ...current };
    if (Object.keys(migrated).length) savePos();
    for (const key of LEGACY_KEYS) {
      try { localStorage.removeItem(key); } catch (err) { /* 忽略 */ }
    }
  }

  function savePos() {
    try {
      localStorage.setItem(POS_KEY, JSON.stringify(nodePos.value));
    } catch (err) {
      /* 坐标持久化失败不影响业务 */
    }
  }

  function resetAllPos() {
    nodePos.value = {};
    savePos();
  }

  /** 拖拽落点记忆（调用方在 layout() 之外的用户拖动松手时调用）。 */
  function remember(id, x, y) {
    nodePos.value[id] = { x: Math.round(x), y: Math.round(y) };
    savePos();
  }

  return { nodePos, loadPos, savePos, resetAllPos, remember };
}
