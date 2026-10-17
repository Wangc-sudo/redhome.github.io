/**
 * 依赖拉线（2026-10-17 F3 从 FlowPage.vue 抽出，行为零变化）：
 * 模块 → 模块 ＝ 任务依赖——从「先跑」模块的输出端口拉线到「后跑」模块的
 * 输入端口，目标模块全部成员的 depends_on 追加源模块全部成员（逐成员调
 * deps API）；点选依赖线＝解除。落点命中 data-module-in（方向无关）。
 */
import { computed, ref } from 'vue';
import { updatePipelineDeps } from '../api.js';
import { setFlash } from '../store/me.js';
import { draftPath } from './layout.js';

export function useDepWire({
  busy, panX, panY, zoom, taskNodes, canPipelines, reload,
}) {
  const depDraft = ref(null);    // {fromModule, x1, y1, x2, y2}

  function startDepWire(evt, node) {
    if (evt.button !== 0 || busy.value || !canPipelines.value) return;
    evt.stopPropagation();
    evt.preventDefault();
    const canvasEl = evt.target.closest('.canvas');
    const rect = canvasEl.getBoundingClientRect();
    // F5：起点=被按下的端口圆心（DOM 实测，与成员口同手法），
    // 不再钉死节点右缘几何
    const portRect = evt.target.getBoundingClientRect();
    const x1 = (portRect.left + portRect.width / 2 - rect.left - panX.value) / zoom.value;
    const y1 = (portRect.top + portRect.height / 2 - rect.top - panY.value) / zoom.value;
    depDraft.value = { fromModule: node, x1, y1, x2: x1, y2: y1 };
    function onMove(moveEvt) {
      depDraft.value.x2 = (moveEvt.clientX - rect.left - panX.value) / zoom.value;
      depDraft.value.y2 = (moveEvt.clientY - rect.top - panY.value) / zoom.value;
    }
    function onUp(upEvt) {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      const draft = depDraft.value;
      depDraft.value = null;
      const hit = document.elementFromPoint(upEvt.clientX, upEvt.clientY);
      const port = hit && hit.closest ? hit.closest('[data-module-in]') : null;
      if (!port) return;
      const targetId = port.getAttribute('data-module-in');
      const target = taskNodes.value.find((n) => n.id === targetId);
      if (target) dropDepWire(draft.fromModule, target);
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  async function dropDepWire(fromModule, toModule) {
    if (fromModule.id === toModule.id) return;
    const sourceIds = fromModule.services.map((s) => s.service_id);
    const ok = window.confirm(
      `建立依赖：「${toModule.label}」的全部任务 依赖 「${fromModule.label}」？\n`
      + `（${toModule.services.map((s) => s.service_id).join('、')} `
      + `depends_on += ${sourceIds.join('、')}）`,
    );
    if (!ok) return;
    busy.value = true;
    try {
      for (const service of toModule.services) {
        const next = [...new Set([...(service.depends_on || []), ...sourceIds])];
        await updatePipelineDeps({ service_id: service.service_id, depends_on: next });
      }
      setFlash('ok', `已建立依赖：${toModule.label} ← ${fromModule.label}`);
      await reload(['pipelines']);   // F4：依赖只影响任务域
    } catch (err) {
      setFlash('err', `建立依赖失败：${err.message}`);
    } finally {
      busy.value = false;
    }
  }

  /** 点选依赖线＝解除（目标模块成员 depends_on 移除源模块成员）。 */
  async function onDepEdgeClick(dep) {
    const ok = window.confirm(
      `解除依赖：「${dep.toLabel}」 不再依赖 「${dep.fromLabel}」？`,
    );
    if (!ok) return;
    busy.value = true;
    try {
      for (const service of dep.toModule.services) {
        const next = (service.depends_on || [])
          .filter((d) => !dep.sourceIds.includes(d));
        await updatePipelineDeps({ service_id: service.service_id, depends_on: next });
      }
      setFlash('ok', `已解除依赖：${dep.toLabel} ✕ ${dep.fromLabel}`);
      await reload(['pipelines']);   // F4：依赖只影响任务域
    } catch (err) {
      setFlash('err', `解除依赖失败：${err.message}`);
    } finally {
      busy.value = false;
    }
  }

  const depDraftPath = computed(() => {
    const w = depDraft.value;
    return w ? draftPath(w.x1, w.y1, w.x2, w.y2) : '';
  });

  return { depDraft, depDraftPath, startDepWire, onDepEdgeClick };
}
