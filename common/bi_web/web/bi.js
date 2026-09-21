"use strict";
/* ========================= 0. 基础工具 ========================= */
var PAL = ['#2f5ce8','#0f9b8e','#8f6d0a','#2f6fed','#a85400','#0a7d4f','#6366f1','#0ea5b7'];
/* 挂零红点色：复用 bi.css --severity-p0（与 miniBar p0 同值），不新造色值。 */
var SEV_P0 = '#cf222e';

function nf(n, d){ if (n == null || !isFinite(n)) return '—'; return n.toLocaleString('zh-CN', {maximumFractionDigits: d == null ? 0 : d}); }
function wan(n, d){ return n == null || !isFinite(n) ? '—' : nf(n / 1e4, d == null ? 1 : d); }
function pct(v, d){ return v == null || !isFinite(v) ? '—' : (v * 100).toFixed(d == null ? 1 : d) + '%'; }
function money(n){ if (n == null || !isFinite(n)) return '—'; var a = Math.abs(n);
  if (a >= 1e8) return nf(n / 1e8, 2); if (a >= 1e4) return nf(n / 1e4, 1); return nf(n, a < 100 ? 1 : 0); }
function moneyUnit(n){ if (n == null || !isFinite(n)) return ''; var a = Math.abs(n); return a >= 1e8 ? '亿' : (a >= 1e4 ? '万' : ''); }
function deltaHtml(d){ if (d == null || !isFinite(d)) return '';
  var c = d > 0 ? 'up' : (d < 0 ? 'down' : 'flat'); var s = (d > 0 ? '+' : '') + d.toFixed(1) + '%';
  return '<span class="delta ' + c + '">' + s + '</span>'; }
function el(tag, cls, html){ var e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }
function esc(s){ return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
function sum(arr){ var t = 0; for (var i = 0; i < arr.length; i++) t += (arr[i] || 0); return t; }

var tipEl = document.getElementById('tip');
function showTip(html, x, y){
  tipEl.innerHTML = html; tipEl.style.opacity = '1';
  var w = tipEl.offsetWidth, h = tipEl.offsetHeight;
  var left = x + 14, top = y - h - 12;
  if (left + w > window.innerWidth - 8) left = x - w - 14;
  if (top < 8) top = y + 16;
  tipEl.style.left = left + 'px'; tipEl.style.top = top + 'px';
}
function hideTip(){ tipEl.style.opacity = '0'; }
var toastTimer = null;
function toast(msg){
  var t = document.getElementById('toast'); t.textContent = msg; t.classList.add('on');
  clearTimeout(toastTimer); toastTimer = setTimeout(function(){ t.classList.remove('on'); }, 1800);
}

/* ========================= 1. 图表：纯 SVG 手绘（零依赖） ========================= */
function svgNode(tag, attrs){
  var n = document.createElementNS('http://www.w3.org/2000/svg', tag);
  for (var k in attrs) if (attrs[k] != null) n.setAttribute(k, attrs[k]);
  return n;
}
function niceMax(v){
  if (!(v > 0)) return 10;
  var e = Math.pow(10, Math.floor(Math.log10(v))), n = v / e;
  var m = n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10;
  return m * e;
}
function yTicks(max, n){
  var out = []; for (var i = 0; i <= n; i++) out.push(max * i / n); return out;
}
function fmtAxis(v){
  if (Math.abs(v) >= 1e8) return nf(v / 1e8, 1) + '亿';
  if (Math.abs(v) >= 1e4) return nf(v / 1e4, v >= 1e6 ? 0 : 1) + '万';
  return nf(v, 0);
}
function legendHtml(series, pal){
  return '<div class="legend">' + series.map(function(s, i){
    return '<span class="lg"><i style="background:' + pal[i % pal.length] + '"></i>' + esc(s.name) + '</span>';
  }).join('') + '</div>';
}
function clear(node){ while (node.firstChild) node.removeChild(node.firstChild); }

/* 折线图 */
function ChartLine(host, opt){
  clear(host);
  var cats = opt.cats, series = opt.series, pal = opt.pal || PAL;
  var W = host.clientWidth || 720, H = opt.height || host.clientHeight || 280;
  var L = 56, R = 14, T = 8, B = 24;
  /* 迷你趋势（KPI 卡内 sparkline）：高度不足时收窄边距并隐藏 Y 轴——
   * 否则 5 条刻度挤在十几像素里，标签必然重叠成一片。 */
  var compact = H < 80;
  if (compact){ L = 6; R = 6; T = 4; B = 16; }
  var maxV = 0;
  series.forEach(function(s){ s.data.forEach(function(v){ if (v > maxV) maxV = v; }); });
  var max = niceMax(maxV * 1.12) || 10;
  var iw = Math.max(10, W - L - R), ih = Math.max(10, H - T - B);
  var svg = svgNode('svg', {viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none'});
  if (!compact){
    yTicks(max, 4).forEach(function(v){
      var y = T + ih - (v / max) * ih;
      svg.appendChild(svgNode('line', {x1: L, y1: y, x2: L + iw, y2: y, class: 'grid-line'}));
      var tx = svgNode('text', {x: L - 8, y: y + 4, class: 'axis-txt', 'text-anchor': 'end'});
      tx.textContent = fmtAxis(v); svg.appendChild(tx);
    });
  }
  var step = cats.length > 1 ? iw / (cats.length - 1) : 0;
  var X = function(i){ return cats.length > 1 ? L + i * step : L + iw / 2; };
  var Y = function(v){ return T + ih - (Math.max(0, v) / max) * ih; };
  var valid = function(v){ return typeof v === 'number' && isFinite(v); };
  series.forEach(function(s, si){
    /* null 不截 0：拆成连续段绘制、缺口断线（与 dashboard.js echarts
     * 版 connectNulls:false 同口径），面积也只铺在有效段下。 */
    var segs = [], cur = [];
    s.data.forEach(function(v, i){
      if (valid(v)) cur.push([X(i), Y(v)]);
      else if (cur.length){ segs.push(cur); cur = []; }
    });
    if (cur.length) segs.push(cur);
    if (opt.area !== false && series.length === 1){
      var cid = 'ga' + si + '_' + Math.random().toString(36).slice(2, 8);
      var defs = svgNode('defs'), lg = svgNode('linearGradient', {id: cid, x1: 0, y1: 0, x2: 0, y2: 1});
      lg.appendChild(svgNode('stop', {offset: '0%', 'stop-color': pal[si % pal.length], 'stop-opacity': .22}));
      lg.appendChild(svgNode('stop', {offset: '100%', 'stop-color': pal[si % pal.length], 'stop-opacity': 0}));
      defs.appendChild(lg); svg.appendChild(defs);
      segs.forEach(function(seg){
        var pts = seg.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
        svg.appendChild(svgNode('polygon', {points: pts + ' ' + seg[seg.length - 1][0].toFixed(1) + ',' + (T + ih) + ' ' + seg[0][0].toFixed(1) + ',' + (T + ih), fill: 'url(#' + cid + ')'}));
      });
    }
    segs.forEach(function(seg){
      if (seg.length < 2){
        /* 孤立点：>32 类目时不画数据点，这里补一个，否则直接跳过（≤32
         * 时下方统一画圆点，避免重复）。 */
        if (cats.length > 32) svg.appendChild(svgNode('circle', {cx: seg[0][0], cy: seg[0][1], r: 2.6, fill: '#fff', stroke: pal[si % pal.length], 'stroke-width': 1.6}));
        return;
      }
      var pts = seg.map(function(p){ return p[0].toFixed(1) + ',' + p[1].toFixed(1); }).join(' ');
      svg.appendChild(svgNode('polyline', {points: pts, fill: 'none', stroke: pal[si % pal.length],
        'stroke-width': 2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round'}));
    });
    if (cats.length <= 32) s.data.forEach(function(v, i){
      if (!valid(v)) return;
      /* 三态：zero → 红点（severity 色填充、加大半径）；线色一律不变，
       * 不整段变红；missing 的 null 点在上面已被 valid() 天然断线跳过。 */
      var zero = !!(opt.status && opt.status[si] && opt.status[si][i] === 'zero');
      svg.appendChild(svgNode('circle', {cx: X(i), cy: Y(v), r: zero ? 3.4 : 2.6,
        fill: zero ? SEV_P0 : '#fff', stroke: pal[si % pal.length], 'stroke-width': 1.6}));
    });
  });
  var everyN = Math.ceil(cats.length / 10);
  cats.forEach(function(c, i){
    if (i % everyN !== 0 && i !== cats.length - 1) return;
    var tx = svgNode('text', {x: X(i), y: T + ih + 16, class: 'axis-txt', 'text-anchor': 'middle'});
    tx.textContent = c; svg.appendChild(tx);
  });
  host.appendChild(svg);
  host.onmousemove = function(ev){
    var box = host.getBoundingClientRect();
    var rel = (ev.clientX - box.left) / box.width * W;
    var i = Math.round((rel - L) / (step || 1));
    i = Math.max(0, Math.min(cats.length - 1, i));
    var html = '<div class="tt">' + esc(cats[i]) + '</div>' + series.map(function(s, si){
      /* 三态 tooltip：zero → 数值后挂既有 .badge-defect「挂零」角标；
       * missing → 「— · 无数据」；status 缺失按 ok 全量渲染（旧行为）。 */
      var st = opt.status && opt.status[si] ? opt.status[si][i] : null;
      var valHtml;
      if (st === 'missing') valHtml = '<b>— · 无数据</b>';
      else {
        valHtml = '<b>' + (opt.fmt ? opt.fmt(s.data[i]) : nf(s.data[i])) + '</b>';
        if (st === 'zero') valHtml += '<span class="badge-defect">挂零</span>';
      }
      return '<div><span class="dot" style="background:' + pal[si % pal.length] + '"></span>' + esc(s.name) +
        ' ' + valHtml + '</div>';
    }).join('');
    showTip(html, ev.clientX, ev.clientY - 10);
  };
  host.onmouseleave = hideTip;
}

/* 柱状图（分组 / 堆叠） */
function ChartBar(host, opt){
  clear(host);
  var cats = opt.cats, series = opt.series, pal = opt.pal || PAL, stacked = !!opt.stacked;
  var W = host.clientWidth || 720, H = opt.height || host.clientHeight || 280;
  var L = 56, R = 14, T = 8, B = 26;
  var maxV = 0;
  if (stacked){
    for (var k = 0; k < cats.length; k++){
      var t = 0; series.forEach(function(s){ t += (s.data[k] || 0); }); if (t > maxV) maxV = t;
    }
  } else {
    series.forEach(function(s){ s.data.forEach(function(v){ if (v > maxV) maxV = v; }); });
  }
  var max = niceMax(maxV * 1.15) || 10;
  var iw = Math.max(10, W - L - R), ih = Math.max(10, H - T - B);
  var svg = svgNode('svg', {viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none'});
  yTicks(max, 4).forEach(function(v){
    var y = T + ih - (v / max) * ih;
    svg.appendChild(svgNode('line', {x1: L, y1: y, x2: L + iw, y2: y, class: 'grid-line'}));
    var tx = svgNode('text', {x: L - 8, y: y + 4, class: 'axis-txt', 'text-anchor': 'end'});
    tx.textContent = fmtAxis(v); svg.appendChild(tx);
  });
  var slot = iw / cats.length, pad = Math.min(18, slot * 0.28);
  var bw = (slot - pad * 2) / (stacked ? 1 : series.length);
  var Y = function(v){ return T + ih - (Math.max(0, v) / max) * ih; };
  for (var ci = 0; ci < cats.length; ci++){
    var base = T + ih;
    for (var si = 0; si < series.length; si++){
      var v = series[si].data[ci] || 0;
      if (stacked){
        var h = (T + ih) - Y(v);
        var x = L + ci * slot + pad;
        var yy = base - h;
        svg.appendChild(svgNode('rect', {x: x, y: yy, width: Math.max(1, slot - pad * 2), height: Math.max(0, h),
          fill: pal[si % pal.length], rx: 2}));
        base = yy;
      } else {
        var x2 = L + ci * slot + pad + si * bw;
        var h2 = (T + ih) - Y(v);
        svg.appendChild(svgNode('rect', {x: x2, y: Y(v), width: Math.max(1, bw - 2), height: Math.max(0, h2),
          fill: pal[si % pal.length], rx: 2}));
      }
    }
  }
  cats.forEach(function(c, i){
    var tx = svgNode('text', {x: L + i * slot + slot / 2, y: T + ih + 16, class: 'axis-txt', 'text-anchor': 'middle'});
    tx.textContent = c; svg.appendChild(tx);
  });
  host.appendChild(svg);
  host.onmousemove = function(ev){
    var box = host.getBoundingClientRect();
    var rel = (ev.clientX - box.left) / box.width * W;
    var i = Math.floor((rel - L) / slot);
    i = Math.max(0, Math.min(cats.length - 1, i));
    var html = '<div class="tt">' + esc(opt.tipTitle ? opt.tipTitle(cats[i]) : cats[i]) + '</div>' + series.map(function(s, si){
      return '<div><span class="dot" style="background:' + pal[si % pal.length] + '"></span>' + esc(s.name) +
        ' <b>' + (opt.fmt ? opt.fmt(s.data[i]) : nf(s.data[i])) + '</b></div>';
    }).join('');
    showTip(html, ev.clientX, ev.clientY - 10);
  };
  host.onmouseleave = hideTip;
}

/* 横向条形图（TOP 排行） */
function ChartHBar(host, opt){
  clear(host);
  var rows = opt.rows.slice(0, (opt.limit || 10));
  var pal = opt.pal || PAL;
  var W = host.clientWidth || 720, H = rows.length * 30 + 12;
  host.style.flex = '0 0 auto'; host.style.height = H + 'px';
  var L = 118, R = 74;
  var max = 0; rows.forEach(function(r){ if (r.value > max) max = r.value; });
  max = max || 1;
  var iw = Math.max(10, W - L - R);
  var svg = svgNode('svg', {viewBox: '0 0 ' + W + ' ' + H, preserveAspectRatio: 'none'});
  rows.forEach(function(r, i){
    var y = i * 30 + 6, w = Math.max(2, iw * (r.value / max));
    var tx = svgNode('text', {x: L - 8, y: y + 15, class: 'axis-txt', 'text-anchor': 'end', style: 'font-size:12px;fill:#57606a'});
    tx.textContent = r.name.length > 9 ? r.name.slice(0, 9) + '…' : r.name;
    svg.appendChild(tx);
    svg.appendChild(svgNode('rect', {x: L, y: y, width: iw, height: 18, fill: '#f2f4f6', rx: 3}));
    svg.appendChild(svgNode('rect', {x: L, y: y, width: w, height: 18, fill: typeof r.color === 'string' ? r.color : pal[i % pal.length], rx: 3}));
    var v = svgNode('text', {x: L + iw + 8, y: y + 14, class: 'axis-txt', style: 'font-size:12px;fill:#1f2933'});
    v.textContent = opt.fmt ? opt.fmt(r.value) : nf(r.value);
    svg.appendChild(v);
  });
  host.appendChild(svg);
  host.onmousemove = function(ev){
    if (ev.target.tagName !== 'rect') { hideTip(); return; }
    showTip('<b>' + esc(ev.target.__n || '') + '</b>', ev.clientX, ev.clientY - 8);
  };
  Array.prototype.forEach.call(svg.querySelectorAll('rect'), function(r, i){
    r.__n = rows[Math.floor(i / 2)] ? rows[Math.floor(i / 2)].name : '';
  });
  host.onmouseleave = hideTip;
}

/* 环形图 */
function ChartDonut(host, opt){
  clear(host);
  var items = opt.items.slice(0), pal = opt.pal || PAL;
  var total = sum(items.map(function(i){ return i.value; })) || 1;
  items.sort(function(a, b){ return b.value - a.value; });
  var wrap = el('div', 'donut-wrap');
  var donut = el('div', 'donut'), lg = el('div', 'donut-lg');
  var box = Math.min(320, (host.clientWidth || 320) * 0.46);
  donut.style.height = Math.max(180, Math.min(300, host.clientHeight || 240)) + 'px';
  var svg = svgNode('svg', {viewBox: '0 0 100 100', width: '100%', height: '100%', style: 'overflow:visible'});
  var cx = 50, cy = 50, r = 34, w = 15, acc = -90;
  items.forEach(function(it, i){
    var ang = it.value / total * 360;
    var c = typeof it.color === 'string' ? it.color : pal[i % pal.length];
    var rr = svgNode('circle', {cx: cx, cy: cy, r: r, fill: 'none', stroke: c, 'stroke-width': w,
      'stroke-dasharray': (ang / 360 * 2 * Math.PI * r) + ' ' + (2 * Math.PI * r),
      transform: 'rotate(' + acc + ' 50 50)'});
    rr.style.cursor = 'pointer';
    rr.addEventListener('mousemove', function(ev){
      showTip('<div class="tt">' + esc(it.name) + '</div><b>' + (opt.fmt ? opt.fmt(it.value) : nf(it.value)) +
        '</b> · ' + pct(it.value / total), ev.clientX, ev.clientY - 10);
    });
    rr.addEventListener('mouseleave', hideTip);
    if (opt.onSelect) rr.addEventListener('click', function(){ opt.onSelect(it, i); });
    svg.appendChild(rr);
    acc += ang;
  });
  var c1 = svgNode('text', {x: 50, y: 48, 'text-anchor': 'middle', style: 'font-size:7px;fill:#667085'});
  c1.textContent = opt.centerLabel || '合计';
  var c2 = svgNode('text', {x: 50, y: 58, 'text-anchor': 'middle', style: 'font-size:9px;font-weight:700;fill:#1f2933'});
  c2.textContent = opt.fmt ? opt.fmt(total) : nf(total);
  svg.appendChild(c1); svg.appendChild(c2);
  donut.appendChild(svg);
  items.forEach(function(it, i){
    var row = el('div', 'row', '<span class="dot" style="background:' + (typeof it.color === 'string' ? it.color : pal[i % pal.length]) + '"></span>' +
      '<span>' + esc(it.name.length > 22 ? it.name.slice(0, 22) + '…' : it.name) + '</span><b>' + (opt.fmt ? opt.fmt(it.value) : nf(it.value)) + '</b>');
    lg.appendChild(row);
  });
  wrap.appendChild(donut); wrap.appendChild(lg); host.appendChild(wrap);
}

/* ========================= 2. 卡片构造器 ========================= */
/* 分段切换器：样式复用既有 .seg / .seg button.on（bi.css L108-111）。
 * opt: {options:[{value,label,disabled,title}], value, onChange, prepend}
 * disabled 档位置灰不可点（inline opacity，不新增类名/色值）。 */
function Seg(host, opt){
  var seg = el('span', 'seg');
  (opt.options || []).forEach(function(op){
    var b = el('button', null, esc(op.label));
    b.type = 'button';
    if (op.value === opt.value) b.className = 'on';
    if (op.disabled){
      b.disabled = true;
      b.style.opacity = '.45';
      b.style.cursor = 'default';
      if (op.title) b.title = op.title;
    }
    if (!op.disabled) b.addEventListener('click', function(){
      Array.prototype.forEach.call(seg.querySelectorAll('button'), function(x){ x.className = ''; });
      b.className = 'on';
      if (opt.onChange) opt.onChange(op.value);
    });
    seg.appendChild(b);
  });
  if (opt.prepend && host.firstChild) host.insertBefore(seg, host.firstChild);
  else host.appendChild(seg);
  return seg;
}
function CardBox(span, heightClass, title, tag, hint){
  var w = el('div', 'widget ' + span);
  var head = el('div', 'widget__head');
  if (hint) head.appendChild(el('span', 'widget__hint', hint));
  /* 批次 B（排序+折叠）：折叠钮 + 拖拽把手。draggable 只挂把手，事件委托
   * 在 bi-data.js 绑 #grid（replaceWidget 整块替换节点，绑 widget 即失效）。
   * 折叠钮与 cursor:grab 走 inline（复用把手同色值 #c3cad6，不动 bi.css、
   * 不加新色值）。 */
  var col = el('span', 'widget__collapse', '▾');
  col.title = '折叠';
  col.style.cssText = 'cursor:pointer;color:#c3cad6;font-size:13px;user-select:none;line-height:1;padding:2px';
  head.appendChild(col);
  var drag = el('span', 'widget__drag', '⠿');
  drag.setAttribute('draggable', 'true');
  drag.style.cursor = 'grab';
  drag.title = '拖拽排序 · 点击弹出移动菜单';
  head.appendChild(drag);
  w.appendChild(head);
  var card = el('div', 'card ' + (heightClass || ''));
  var t = el('div', 'card-title', esc(title) + (tag ? '<span class="tag">' + esc(tag) + '</span>' : ''));
  var body = el('div', 'card-body');
  card.appendChild(t); card.appendChild(body);
  w.appendChild(card);
  return {widget: w, body: body, title: t};
}
function CardHtml(span, heightClass, title, tag, inner){
  var b = CardBox(span, heightClass, title, tag);
  b.body.innerHTML = inner;
  return b.widget;
}
function CardKpi(o){
  var b = CardBox(o.span || 'c3', 'h-kpi', o.title, o.tag);
  var scale = moneyUnit(o.value);
  var val = money(o.value), unit = o.unit != null ? (scale ? scale + o.unit : o.unit) : scale;
  var html = '<div class="kpi-row"><div class="kpi-value' + (o.onClick ? ' click' : '') + '">' + val +
    '<span class="u">' + unit + '</span></div></div>';
  if (o.delta != null) html += '<div class="kpi-sub">' + (o.compare_label || '日环比') + ' ' + deltaHtml(o.delta) + '</div>';
  if (o.progress != null){
    var cls = o.severity && o.severity !== 'ok' ? o.severity : '';
    html += '<div class="progress"><div class="progress-bar ' + cls + '" style="width:' +
      Math.min(100, Math.max(0, o.progress * 100)).toFixed(1) + '%"></div></div>' +
      '<div class="kpi-sub">达成 ' + pct(o.progress) + (o.progressNote ? ' · ' + esc(o.progressNote) : '') + '</div>';
  }
  if (o.sub) html += '<div class="kpi-sub">' + o.sub + '</div>';
  b.body.innerHTML = html;
  if (o.onClick){
    var n = b.body.querySelector('.kpi-value');
    n.addEventListener('click', function(){ o.onClick(); });
    n.title = '点击下钻到明细';
  }
  return b.widget;
}
function fmtCell(v, f){
  if (v == null || v === '') return '—';
  switch (f){
    case 'wan': return nf(v / 1e4, 1);
    case 'wan0': return nf(v / 1e4, 0);
    case 'int': return nf(v, 0);
    case 'ratio': return nf(v, 2);
    case 'pct': case 'percent': return pct(v);
    case 'money': return money(v);
    case 'plus': return (v > 0 ? '+' : '') + nf(v / 1e4, 1);
    default: return String(v);
  }
}
/* cols: [{key,label,format,sum,cls,bar}] ; opts:{onRowClick,rowTitle,footLabel,stickyHead} */
function CardTable(o){
  var b = CardBox(o.span || 'c12', o.heightClass || '', o.title, o.tag);
  var cols = o.cols, rows = o.rows;
  var scroll = el('div', 'table-scroll');
  var html = '<table class="data-table"><thead><tr>';
  cols.forEach(function(c){ html += '<th>' + esc(c.label) + '</th>'; });
  html += '</tr></thead><tbody>';
  if (!rows.length){
    html += '<tr><td colspan="' + cols.length + '" style="text-align:center;padding:24px 0;color:#667085">暂无数据</td></tr>';
  }
  rows.forEach(function(r, ri){
    html += '<tr' + (o.onRowClick ? ' class="clk"' : '') + ' data-i="' + ri + '">';
    cols.forEach(function(c, ci){
      var v = r[c.key];
      var cls = ci === 0 ? '' : 'num';
      if (c.cls) cls += ' ' + (typeof c.cls === 'function' ? c.cls(v, r) : c.cls);
      var inner;
      if (c.render) inner = c.render(v, r, ci);
      else if (c.chip) inner = v ? '<span class="alert-chip ' + String(v) + '">' + esc(c.chipMap ? c.chipMap[v] : v) + '</span>' : '<span class="muted">—</span>';
      else inner = fmtCell(v, c.format);
      if (ci === 0 && c.badge && r[c.badge] === false) inner += '<span class="badge-defect" title="本月截至今日无销单">挂零</span>';
      if (ci === 0 && !cols.some(function(x){ return x.chip; }) && !cols.some(function(x){ return x.badge; }) && r.__alert){
        inner += '<span class="alert-chip ' + r.__alert + '">' + esc(ALERT_MAP[r.__alert] || '') + '</span>';
      }
      html += '<td class="' + cls + '"' + (ci === 0 && typeof v === 'string' ? ' title="' + esc(v) + '"' : '') + '>' + inner + '</td>';
    });
    html += '</tr>';
  });
  html += '</tbody>';
  if (o.foot !== false){
    var fr = o.footRows ? rows.filter(o.footRows) : rows;
    html += '<tfoot><tr>';
    cols.forEach(function(c, ci){
      if (ci === 0){ html += '<td>' + esc(o.footLabel || '合计') + '</td>'; return; }
      if (c.sum){
        var s = sum(fr.map(function(r){ return r[c.key] || 0; }));
        html += '<td>' + fmtCell(fr.length ? s : null, c.format) + '</td>';
      } else html += '<td>—</td>';
    });
    html += '</tr></tfoot>';
  }
  html += '</table>';
  scroll.innerHTML = html;
  b.body.appendChild(scroll);
  if (o.onRowClick){
    Array.prototype.forEach.call(scroll.querySelectorAll('tbody tr'), function(tr){
      tr.addEventListener('click', function(){ o.onRowClick(rows[+tr.getAttribute('data-i')]); });
      tr.title = o.rowTitle || '点击下钻';
    });
  }
  return b.widget;
}
var ALERT_MAP = {p0: '⛔ P0', p1: '⚠️ P1', p2: 'ℹ️ P2', ok: '✅ 正常'};
function chipHtml(sv, map){
  return '<span class="alert-chip ' + sv + '">' + esc(map && map[sv] ? map[sv] : (ALERT_MAP[sv] || sv)) + '</span>';
}

/* ========================= 3. 图表卡片构造器 ========================= */
var drawers = [];
function runDrawers(){ var d = drawers.slice(); drawers = []; d.forEach(function(f){ try { f(); } catch (e) { console.error(e); } }); }
function CardLine(o){
  var b = CardBox(o.span || 'c8', 'h-chart', o.title, o.tag, o.hint);
  b.body.appendChild(el('div', 'legend', legendHtml(o.series, o.pal || PAL)));
  var host = el('div', 'chart-host'); b.body.appendChild(host);
  drawers.push(function(){ ChartLine(host, {cats: o.cats, series: o.series, fmt: o.fmt, pal: o.pal, area: o.area, status: o.status}); });
  return b.widget;
}
function CardBar(o){
  var b = CardBox(o.span || 'c6', 'h-chart', o.title, o.tag, o.hint);
  b.body.appendChild(el('div', 'legend', legendHtml(o.series, o.pal || PAL)));
  var host = el('div', 'chart-host'); b.body.appendChild(host);
  drawers.push(function(){ ChartBar(host, {cats: o.cats, series: o.series, fmt: o.fmt, pal: o.pal, stacked: o.stacked, tipTitle: o.tipTitle}); });
  return b.widget;
}
function CardDonut(o){
  var b = CardBox(o.span || 'c6', 'h-chart', o.title, o.tag, o.hint);
  var host = el('div', 'chart-host'); b.body.appendChild(host);
  drawers.push(function(){ ChartDonut(host, {items: o.items, fmt: o.fmt, centerLabel: o.centerLabel, pal: o.pal, onSelect: o.onSelect}); });
  return b.widget;
}
function CardHBar(o){
  var b = CardBox(o.span || 'c6', o.heightClass || 'h-chart', o.title, o.tag, o.hint);
  var host = el('div', 'chart-host'); b.body.appendChild(host);
  drawers.push(function(){ ChartHBar(host, {rows: o.rows, fmt: o.fmt, pal: o.pal, limit: o.limit}); });
  return b.widget;
}
function miniBar(rate, cls){
  return '<span style="display:inline-block;width:96px;height:8px;background:#e9edf1;border-radius:99px;vertical-align:middle;overflow:hidden">' +
    '<span style="display:block;height:100%;width:' + Math.min(100, Math.max(0, rate * 100)).toFixed(1) + '%;background:' +
    (cls === 'p0' ? '#cf222e' : cls === 'p1' ? '#a85400' : cls === 'p2' ? '#8f6d0a' : cls === 'ok' ? '#0a7d4f' : '#2f5ce8') +
    ';border-radius:99px"></span></span>';
}
var SEV_LABEL = {p0: '已断货', p1: '低库存', p2: '偏低', ok: '正常'};
