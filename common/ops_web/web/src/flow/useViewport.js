/**
 * 画布视口（2026-10-17 F3 从 FlowPage.vue 抽出）：滚轮缩放（光标锚点）、
 * 拖空白平移、节点头拖拽移动、复位与锚点聚焦。行为与旧实现一致。
 */
import { computed, nextTick, ref } from 'vue';

const ZOOM_MIN = 0.3;
const ZOOM_MAX = 2;

export function useViewport() {
  const zoom = ref(1);
  const panX = ref(0);
  const panY = ref(0);

  const viewportStyle = computed(() => ({
    transform: `translate(${panX.value}px, ${panY.value}px) scale(${zoom.value})`,
  }));
  const gridStyle = computed(() => ({
    backgroundSize: `${22 * zoom.value}px ${22 * zoom.value}px`,
    backgroundPosition: `${panX.value}px ${panY.value}px`,
  }));

  function onWheel(evt) {
    const old = zoom.value;
    const next = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN,
      old * (evt.deltaY < 0 ? 1.12 : 1 / 1.12)));
    if (next === old) return;
    const rect = evt.currentTarget.getBoundingClientRect();
    const cx = evt.clientX - rect.left;
    const cy = evt.clientY - rect.top;
    panX.value = cx - ((cx - panX.value) / old) * next;
    panY.value = cy - ((cy - panY.value) / old) * next;
    zoom.value = next;
  }

  function startPan(evt) {
    if (evt.button !== 0) return;
    const startX = evt.clientX;
    const startY = evt.clientY;
    const originX = panX.value;
    const originY = panY.value;
    function onMove(moveEvt) {
      panX.value = originX + moveEvt.clientX - startX;
      panY.value = originY + moveEvt.clientY - startY;
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  function resetView() {
    zoom.value = 1;
    panX.value = 0;
    panY.value = 0;
  }

  /**
   * 节点头拖拽移动；onDrop(node) 在松手时回调（调用方持久化坐标）。
   * 直接改写 node.x/y（节点是图模型对象，实时驱动连线端点）。
   */
  function startNodeDrag(evt, node, { onDrop } = {}) {
    if (evt.button !== 0) return;
    evt.stopPropagation();
    const startX = evt.clientX;
    const startY = evt.clientY;
    const originX = node.x;
    const originY = node.y;
    function onMove(moveEvt) {
      node.x = Math.max(0, originX + (moveEvt.clientX - startX) / zoom.value);
      node.y = Math.max(0, originY + (moveEvt.clientY - startY) / zoom.value);
    }
    function onUp() {
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
      if (onDrop) onDrop(node);
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }

  /** 锚点聚焦：把 (x, y) 画布坐标平移到视口 (offsetX, offsetY) 处。 */
  async function focusOn(x, y, { offsetX = 260, offsetY = 120 } = {}) {
    await nextTick();
    panX.value = offsetX - x * zoom.value;
    panY.value = offsetY - y * zoom.value;
  }

  return {
    zoom, panX, panY,
    viewportStyle, gridStyle,
    onWheel, startPan, resetView, startNodeDrag, focusOn,
  };
}
