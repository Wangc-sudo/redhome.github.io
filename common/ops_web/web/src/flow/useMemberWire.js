/**
 * 成员拉线（2026-10-17 F3 从 FlowPage.vue 抽出，行为零变化）。
 * 语义配对：成员口 → 区域口＝归口该区（部门取通讯录原值）；
 * 成员口 → 部门口＝归口该区该部门；区域成员口 → 他区部门/区域口＝搬迁
 * （moveMember，confirm+双侧名册同步）；区域成员口 → 本区部门口＝区内调部门。
 * 起点=被按下的端口圆心（DOM 实测画布坐标），落点=elementFromPoint 命中
 * data-region-in / data-dept-in 端口（方向无关）。
 */
import { computed, ref } from 'vue';
import { assignMember, moveMember } from '../api.js';
import { setFlash } from '../store/me.js';
import { edgePath } from './layout.js';

export function useMemberWire({
  busy, panX, panY, zoom, regionNodes, canWire, reload,
}) {
  const wireDraft = ref(null);   // {member, x1, y1, x2, y2}

  function regionLabel(key) {
    const hit = regionNodes.value.find((n) => n.key === key);
    return hit ? hit.label : key;
  }

  function startWire(evt, member) {
    if (evt.button !== 0 || busy.value) return;
    if (!canWire.value) return;
    evt.stopPropagation();
    evt.preventDefault();
    const canvasEl = evt.target.closest('.canvas');
    const rect = canvasEl.getBoundingClientRect();
    // 起点=被按下的端口圆心（画布坐标），不再绑死池节点几何
    const portRect = evt.target.getBoundingClientRect();
    const x1 = (portRect.left + portRect.width / 2 - rect.left - panX.value) / zoom.value;
    const y1 = (portRect.top + portRect.height / 2 - rect.top - panY.value) / zoom.value;
    wireDraft.value = { member, x1, y1, x2: x1, y2: y1 };
    function toCanvas(moveEvt) {
      return {
        x: (moveEvt.clientX - rect.left - panX.value) / zoom.value,
        y: (moveEvt.clientY - rect.top - panY.value) / zoom.value,
      };
    }
    function onMove(moveEvt) {
      const p = toCanvas(moveEvt);
      wireDraft.value.x2 = p.x;
      wireDraft.value.y2 = p.y;
    }
    function onUp(upEvt) {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      const draft = wireDraft.value;
      wireDraft.value = null;
      const hit = document.elementFromPoint(upEvt.clientX, upEvt.clientY);
      if (!hit || !hit.closest) return;
      const deptPort = hit.closest('[data-dept-in]');
      if (deptPort) {
        dropWire(deptPort.getAttribute('data-dept-in'), draft.member);
        return;
      }
      const regionPort = hit.closest('[data-region-in]');
      if (regionPort) {
        dropWire(regionPort.getAttribute('data-region-in'), draft.member);
      }
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  /** 落点解析："regionKey"（区域口）或 "regionKey|deptId|deptName"（部门口）。 */
  async function dropWire(target, member) {
    const [regionKey, deptId, deptName] = target.split('|');
    const region = regionNodes.value.find((n) => n.key === regionKey);
    if (!region) return;
    const fromRegion = member.region || null;   // 区域成员卡带 region 标记
    const name = member.name || member.user_id;
    const deptText = deptName ? `·${deptName}` : '';
    const moving = fromRegion && fromRegion !== regionKey;

    const ok = window.confirm(moving
      ? `将「${name}」从 ${regionLabel(fromRegion)} 搬迁到 ${region.label}${deptText}？\n\n`
        + '跨区搬迁会同步增删双侧名册，影响报数资格与当月目标归属。'
      : `将「${name}」归入 ${region.label}${deptText}？`);
    if (!ok) return;

    busy.value = true;
    try {
      if (moving) {
        await moveMember({
          user_id: member.user_id,
          to_region: regionKey,
          person_name: member.name || null,
          dept_name: deptName || member.dept_name || '',
          from_region: fromRegion,
          note: '',
        });
        setFlash('ok', `已搬迁「${name}」到 ${region.label}${deptText}`);
      } else {
        await assignMember({
          user_id: member.user_id,
          region: regionKey,
          dept_id: deptId || member.dept_id || null,
          dept_name: deptName || member.dept_name || '',
          name: member.name || null,
        });
        setFlash('ok', `已把「${name}」归入 ${region.label}${deptText}`);
      }
      // F4 按域局部刷新：归口/搬迁只影响组织归属（overview）与名册计数（roster）
      await reload(['overview', 'roster']);
    } catch (err) {
      setFlash('err', `操作失败：${err.message}`);
    } finally {
      busy.value = false;
    }
  }

  const wireDraftPath = computed(() => {
    const w = wireDraft.value;
    return w ? edgePath(w.x1, w.y1, w.x2, w.y2) : '';
  });

  return { wireDraft, wireDraftPath, startWire };
}
