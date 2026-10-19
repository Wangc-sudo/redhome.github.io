/**
 * 画布抽屉状态机（2026-10-16 F2）：drawer = {kind, node?}。
 * kind ∈ task | region | calendar | records | grants | datafix | governance
 * | relay（2026-10-19 播报试投）；
 * 全局节点 id → kind 的映射也收口在这里。
 */
import { ref } from 'vue';

const GLOBAL_NODE_KIND = {
  'global:calendar': 'calendar',
  'global:records': 'records',
  'global:grants': 'grants',
  'global:datafix': 'datafix',
  'global:governance': 'governance',
  'global:relay': 'relay',
};

export function globalNodeKind(nodeId) {
  return GLOBAL_NODE_KIND[nodeId] || null;
}

export function useDrawer() {
  const drawer = ref(null);

  function openDrawer(kind, node) {
    drawer.value = node ? { kind, node } : { kind };
  }

  function closeDrawer() {
    drawer.value = null;
  }

  return { drawer, openDrawer, closeDrawer };
}
