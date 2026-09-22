/* bi-data.js 数据适配层（2026-09-17 原稿转正）：原稿壳（bi.js/bi.html）+
 * /api/v1/* 驱动渲染。语义照抄 legacy web/dashboard.js：
 * pathname 取看板 id → dashboards 导航 → definition 页头/筛选 → 逐卡取数
 * （cache:"no-store"，WeakMap 竞态防护），payload.chart 分派到原稿卡片
 * 构造器；400/非 200 → 卡内错误态，整页不挂。零依赖，ES5。
 * 契约（bi.js 提供）：esc nf wan pct money moneyUnit deltaHtml el clear
 * toast Seg CardBox CardKpi CardLine CardBar CardDonut CardTable CardHtml
 * ChartLine miniBar runDrawers PAL；挂载点 id：nav pgTitle pgSub pgNote
 * pgFilters grid footAsOf tip toast。粒度（2026-09-18）：卡级覆盖 >
 * 页级默认 > 卡片 default_gran，params 白名单求交；status 三态透传，
 * 缺省按 ok 全量渲染（旧行为）。布局（2026-09-18 批次 B）：#grid 事件
 * 委托拖拽排序 + 折叠 + 页头「恢复默认」；只换 DOM 顺序不重拉数据，
 * localStorage["bi.layout.v1.{dashboard_id}"] = {order, hidden}。
 */
"use strict";

var ERROR_TEXT = '加载失败，请稍后重试';
var EMPTY_TEXT = '暂无数据';
var LOADING_TEXT = '加载中…';
var PLACEHOLDER = '—';
var ALL_TEXT = '全部';
var NO_FACT_TEXT = '应接入未接入 · 数据待接入';
var DEFAULT_REFRESH_SECONDS = 300;

/* severity chip 文案（原稿 chipMap 风格）。 */
var CHIP_MAP = {p0: 'P0 严重', p1: 'P1 超期', p2: 'P2 关注', ok: '正常'};

var currentFilters = {};   /* param → 当前值（空串 = 全部） */
var records = [];          /* 卡片记录 {placement, index, widget} */
var loadToken = new WeakMap(); /* 每卡取数代次：慢到的旧响应不得覆盖新响应 */
var footAsOfIndex = -1;    /* 页脚 as_of 取自序号最小的带 as_of/date 卡 */
var currentDashId = null;  /* initShell 写入，供 buildFilters/粒度 Seg 闭包取数 */
var pageGran = null;       /* 页级默认粒度；null = 未选，各卡走自己的 default_gran */
var cardGran = {};         /* 卡级粒度覆盖：card_id → gran */
var pageFallback = {};     /* 页级档触发降级后按卡记住回落：card_id → default_gran */
var GRAN_OPTIONS = [{value: 'day', label: '日'}, {value: 'week', label: '周'}, {value: 'month', label: '月'}, {value: 'year', label: '年'}];
var GRAN_TEXT = {day: '日', week: '周', month: '月', year: '年'};
var lastDef = null;      /* 最近一次 definition：「布局 · 恢复默认」重建用 */
var layoutHidden = {};   /* 折叠态：card_id → true；持久化进 layout.hidden */

/* ---- 基础 ---------------------------------------------------------------- */

function fetchJson(url){
  return fetch(url, {cache: 'no-store'}).then(function(response){
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  });
}

function dashboardId(){
  var m = location.pathname.match(/^\/d\/([^/]+)/);
  return m ? decodeURIComponent(m[1]) : 'l1-cockpit';
}

function moneyFmt(unit){
  return unit === '元' ? function(v){ return v == null ? PLACEHOLDER : wan(v) + ' 万'; } : null;
}

/* ---- 导航 ---------------------------------------------------------------- */

/* 导航图标由后端配置层下发（「前端逻辑后端化」裁决，2026-09-17）：
 * definition.icon 为 '/static/...' 路径 → <img>（FTP 资源仓的 SVG 矢量图）；
 * 其余非空串 → emoji 文本；空串 → 回退 '📄'（不炸、显眼好排查）。 */
function navIconHtml(icon){
  if (icon && icon.charAt(0) === '/'){
    return '<img class="ic ic-img" src="' + esc(icon) + '" alt="">';
  }
  return '<i class="ic">' + esc(icon || '📄') + '</i>';
}

function buildNav(list, currentId){
  var nav = document.getElementById('nav');
  clear(nav);
  /* 分组由后端 definition.group 驱动（「前端逻辑后端化」裁决，2026-09-17）：
   * 按首次出现顺序建组；空 group 防御性回退「专项分析」（现状全是 L2）。 */
  var buckets = {}, order = [];
  list.forEach(function(d){
    var g = d.group || '专项分析';
    if (!buckets[g]){ buckets[g] = []; order.push(g); }
    buckets[g].push(d);
  });
  order.forEach(function(g){
    nav.appendChild(el('div', 'nav-group', esc(g)));
    buckets[g].forEach(function(d){
      var a = document.createElement('a');
      a.href = '/d/' + encodeURIComponent(d.id);
      a.title = (d.title || d.id) + ' · ' + d.id;
      if (d.id === currentId) a.className = 'active';
      a.innerHTML = navIconHtml(d.icon) +
        '<span>' + esc(d.title || d.id) + '</span>';
      nav.appendChild(a);
    });
  });
}

/* ---- 页头与筛选条 --------------------------------------------------------- */

function refreshNote(seconds){
  if (!seconds) return '';
  if (seconds >= 86400) return '每日自动刷新';
  if (seconds >= 3600) return '每 ' + Math.round(seconds / 3600) + ' 小时自动刷新';
  if (seconds >= 60) return '每 ' + Math.round(seconds / 60) + ' 分钟自动刷新';
  return '每 ' + seconds + ' 秒自动刷新';
}

var resetArmTimer = null;  /* 「恢复默认」二次确认：首次点击只 toast 并武装 3s */

function renderHead(def){
  document.getElementById('pgTitle').textContent = def.title || '';
  document.getElementById('pgSub').textContent =
    (def.cards || []).length + ' 张卡片 · 数据口径 T+1';
  var note = document.getElementById('pgNote');
  note.textContent = refreshNote(def.refresh_seconds);
  /* 「布局 · 恢复默认」+ 二次确认 toast：清 localStorage 后按 def 原序重建。
   * 布局不进 URL；按钮复用既有 .btn（样式微调用 inline，不动 bi.css）。 */
  var btn = el('button', 'btn', '布局 · 恢复默认');
  btn.type = 'button';
  btn.style.cssText = 'display:block;margin:6px 0 0 auto;height:26px;padding:0 12px;font-size:12px';
  btn.addEventListener('click', function(){
    if (resetArmTimer){
      clearTimeout(resetArmTimer);
      resetArmTimer = null;
      resetLayout();
    } else {
      toast('再次点击确认恢复默认布局');
      resetArmTimer = setTimeout(function(){ resetArmTimer = null; }, 3000);
    }
  });
  note.appendChild(btn);
}

function resetLayout(){
  try{ localStorage.removeItem(layoutKey()); }catch(e){}
  layoutHidden = {};
  if (lastDef){
    buildCards(lastDef);
    records.forEach(function(rec){ loadCard(currentDashId, rec); });
  }
  toast('已恢复默认布局');
}

/* 一个筛选下拉：label + select（首项「全部」= 空值），选项来自
 * /api/v1/options/{source}；变更只重拉 params 声明过该 param 的卡。 */
function buildFilters(def){
  var bar = document.getElementById('pgFilters');
  clear(bar);
  /* 页级粒度条：仅当本页存在多档卡（grans.length > 1）时渲染；
   * 变更 → 重拉所有多档卡（卡级覆盖在 effectiveGran 里优先）。 */
  var hasMultiGran = (def.cards || []).some(function(c){ return (c.grans || []).length > 1; });
  if (hasMultiGran){
    var granLab = el('label', 'filter');
    granLab.appendChild(el('span', 'filter-label', '粒度'));
    bar.appendChild(granLab);
    Seg(granLab, {options: GRAN_OPTIONS, value: pageGran, onChange: function(v){
      pageGran = v;
      pageFallback = {}; /* 用户显式改页级档 = 新一轮，清掉旧的回落记忆 */
      records.forEach(function(rec){
        if ((rec.placement.grans || []).length > 1) loadCard(currentDashId, rec);
      });
    }});
  }
  var specs = def.filters || [];
  return Promise.all(specs.map(function(spec){
    return fetchJson('/api/v1/options/' + encodeURIComponent(spec.source)).then(function(payload){
      var lab = el('label', 'filter');
      lab.appendChild(el('span', 'filter-label', esc(spec.label || spec.param)));
      var sel = document.createElement('select');
      sel.setAttribute('data-param', spec.param);
      var all = document.createElement('option');
      all.value = ''; all.textContent = ALL_TEXT;
      sel.appendChild(all);
      (payload.options || []).forEach(function(v){
        var op = document.createElement('option');
        op.value = v; op.textContent = v;
        sel.appendChild(op);
      });
      sel.addEventListener('change', function(){
        currentFilters[spec.param] = sel.value;
        records.forEach(function(rec){
          var ps = rec.placement.params || [];
          if (ps.indexOf(spec.param) >= 0) loadCard(currentDashId, rec);
        });
      });
      lab.appendChild(sel);
      bar.appendChild(lab);
    });
  }));
}

/* ---- 卡片占位与取数 -------------------------------------------------------- */

function buildCards(def){
  var grid = document.getElementById('grid');
  clear(grid);
  footAsOfIndex = -1;
  records = (def.cards || []).map(function(placement, index){
    var box = CardBox('c' + (placement.span || 12), '', placement.title || placement.card);
    box.widget.setAttribute('data-card', placement.card);
    box.body.innerHTML = '<div class="empty">' + esc(LOADING_TEXT) + '</div>';
    grid.appendChild(box.widget);
    return {placement: placement, index: index, widget: box.widget};
  });
  applySavedLayout();
}

/* ---- 布局：排序 + 折叠（批次 B §5） ------------------------------------------
 * 持久化 localStorage["bi.layout.v1.{dashboard_id}"] = {order:[card_id],
 * hidden:[card_id]}；读取时与 def.cards 求交：未知 id 丢弃、新增 id 追加
 * 末尾（后端增删卡绝不能让前端白屏）。只换 DOM 顺序，不触发数据重拉。 */

function layoutKey(){ return 'bi.layout.v1.' + currentDashId; }

function readLayout(){
  try{
    var o = JSON.parse(localStorage.getItem(layoutKey()) || 'null');
    if (o && typeof o === 'object') return {order: o.order || [], hidden: o.hidden || []};
  }catch(e){}
  return {order: [], hidden: []};
}

function persistLayout(){
  var grid = document.getElementById('grid');
  var order = [];
  Array.prototype.forEach.call(grid.children, function(w){
    var id = w.getAttribute && w.getAttribute('data-card');
    if (id) order.push(id);
  });
  var hidden = [];
  records.forEach(function(rec){
    if (layoutHidden[rec.placement.card]) hidden.push(rec.placement.card);
  });
  try{ localStorage.setItem(layoutKey(), JSON.stringify({order: order, hidden: hidden})); }catch(e){}
}

function applyCollapsed(widget, collapsed){
  var card = widget.querySelector('.card');
  if (card) card.style.display = collapsed ? 'none' : '';
  var btn = widget.querySelector('.widget__collapse');
  if (btn){
    btn.textContent = collapsed ? '▸' : '▾';
    btn.title = collapsed ? '展开' : '折叠';
  }
}

function toggleCollapse(widget){
  var id = widget.getAttribute('data-card');
  if (!id) return;
  var collapsed = !layoutHidden[id];
  if (collapsed) layoutHidden[id] = true; else delete layoutHidden[id];
  applyCollapsed(widget, collapsed);
  persistLayout();
}

/* buildCards 后按 localStorage 顺序 appendChild 重排，折叠态一并恢复。 */
function applySavedLayout(){
  var grid = document.getElementById('grid');
  var saved = readLayout();
  layoutHidden = {};
  saved.hidden.forEach(function(id){ layoutHidden[id] = true; });
  var byId = {};
  records.forEach(function(rec){ byId[rec.placement.card] = rec; });
  var ordered = [];
  saved.order.forEach(function(id){
    if (byId[id]){ ordered.push(byId[id]); delete byId[id]; }
  });
  records.forEach(function(rec){
    if (byId[rec.placement.card]) ordered.push(rec);
  });
  records = ordered;
  records.forEach(function(rec){
    grid.appendChild(rec.widget);
    applyCollapsed(rec.widget, !!layoutHidden[rec.placement.card]);
  });
}

/* 某卡的有效粒度：卡级覆盖 > 页级默认 > 卡片 default_gran；
 * 仍受 params 白名单与 grans 值域双重约束——未开通的卡（params 无 gran
 * 或 grans 为空）返回 null，请求自动不带 gran；页级/卡级选了该卡不
 * 支持的档位时回落 default_gran（单档卡 thus 不受页级粒度条影响）。 */
function effectiveGran(rec){
  var p = rec.placement;
  var grans = p.grans || [];
  if ((p.params || []).indexOf('gran') < 0 || !grans.length) return null;
  var g = cardGran[p.card];
  if (g && grans.indexOf(g) >= 0) return g;
  var fb = pageFallback[p.card];
  if (fb && grans.indexOf(fb) >= 0) return fb;
  if (pageGran && grans.indexOf(pageGran) >= 0) return pageGran;
  return p.default_gran || null;
}

/* 某卡实际要带的参数：当前筛选值 ∩ 该卡 params 白名单；空值不传。
 * gran 例外：取 effectiveGran（retry 时强制 default_gran）。 */
function cardUrl(dashId, rec, granOverride){
  var base = '/api/v1/d/' + encodeURIComponent(dashId) +
    '/cards/' + encodeURIComponent(rec.placement.card);
  var allowed = rec.placement.params || [];
  var qs = [];
  allowed.forEach(function(name){
    var v = name === 'gran' ? (granOverride || effectiveGran(rec)) : currentFilters[name];
    if (v != null && v !== '') qs.push(encodeURIComponent(name) + '=' + encodeURIComponent(v));
  });
  return qs.length ? base + '?' + qs.join('&') : base;
}

/* retry=true 表示这是「非默认 gran 失败后回退 default_gran」的重拉，
 * 只允许发生一次；再失败才 showCardError（不做静默失败）。 */
function loadCard(dashId, rec, retry){
  var gran = retry ? (rec.placement.default_gran || null) : effectiveGran(rec);
  var token = (loadToken.get(rec) || 0) + 1;
  loadToken.set(rec, token);
  fetchJson(cardUrl(dashId, rec, retry ? gran : null)).then(function(payload){
    if (loadToken.get(rec) !== token) return;
    if (retry){
      /* 回退成功（main 裁定 2026-09-18）：卡级覆盖触发的清覆盖；
       * 页级档触发的按卡记住回落（不动 pageGran）。之后刷新直接走
       * default_gran，不再重复 toast。 */
      if (cardGran[rec.placement.card]) delete cardGran[rec.placement.card];
      else pageFallback[rec.placement.card] = rec.placement.default_gran;
    }
    renderCard(rec, payload);
  }).catch(function(){
    if (loadToken.get(rec) !== token) return;
    var def = rec.placement.default_gran;
    if (!retry && gran && def && gran !== def){
      toast('已回退到' + (GRAN_TEXT[def] || def) + '视图');
      loadCard(dashId, rec, true);
      return;
    }
    showCardError(rec);
  });
}

/* 卡级粒度控件：挂在 widget__head 的 widget__hint 位置（head 最前）。
 * grans 之外的档位渲染为 disabled + title 提示，不隐藏；单档卡（如仅
 * month）其余档位置灰、不可切换。每次 renderCard 后重挂（widget 被
 * replaceWidget 整块替换）。title 按 grans 生成：单档说明数据源边界，
 * 多档说明该档未开通（不再写死「仅支持月度」）。 */
function attachGranSeg(rec){
  var p = rec.placement;
  var grans = p.grans || [];
  if (!grans.length) return;
  var head = rec.widget.querySelector('.widget__head');
  if (!head) return;
  var single = grans.length === 1;
  var supported = grans.map(function(g){ return GRAN_TEXT[g] || g; }).join('/');
  var opts = GRAN_OPTIONS.map(function(g){
    var ok = grans.indexOf(g.value) >= 0;
    return {
      value: g.value, label: g.label, disabled: !ok,
      title: ok ? '' : (single
        ? '该模块数据源仅支持' + (grans[0] === 'month' ? '月度' : supported + '粒度')
        : '该模块暂未开通' + g.label + '档（支持' + supported + '）')
    };
  });
  Seg(head, {options: opts, value: effectiveGran(rec), prepend: true, onChange: function(v){
    cardGran[p.card] = v;
    loadCard(currentDashId, rec);
  }});
}

function replaceWidget(rec, widget){
  /* 重渲染只换卡体：widget 在原位被整块替换，DOM 顺序不动（与拖拽排序
   * 互不干扰）；data-card 与折叠态要在新节点上恢复。 */
  widget.setAttribute('data-card', rec.placement.card);
  if (dragWidget === rec.widget) dragWidget = widget; /* 定时刷新撞上拖拽中：锚点跟到新节点 */
  rec.widget.parentNode.replaceChild(widget, rec.widget);
  rec.widget = widget;
  applyCollapsed(widget, !!layoutHidden[rec.placement.card]);
}

function showCardError(rec){
  var body = rec.widget.querySelector('.card-body');
  if (body) body.innerHTML = '<div class="empty">' + esc(ERROR_TEXT) + '</div>';
}

function renderEmptyCard(rec, text){
  var p = rec.placement;
  var b = CardBox('c' + (p.span || 12), 'h-chart', p.title || p.card, null);
  b.body.innerHTML = '<div class="empty">' + esc(text) + '</div>';
  replaceWidget(rec, b.widget);
}

/* 页脚数据截至：取序号最小的带 as_of/date 卡的值。 */
function noteAsOf(rec, payload){
  var v = payload.as_of || payload.date;
  if (!v) return;
  if (footAsOfIndex === -1 || rec.index < footAsOfIndex){
    footAsOfIndex = rec.index;
    document.getElementById('footAsOf').textContent = String(v);
  }
}

/* ---- payload → 原稿组件 --------------------------------------------------- */

/* scalar → CardKpi：money/moneyUnit 主值 + unit；target → miniBar + pct(rate)；
 * delta_pct（小数比率）→ deltaHtml（百分数）；date/as_of → tag；
 * trend7 → 卡内迷你 ChartLine（cats 取 MM-DD，null 原样传：原稿绘点截 0、
 * tooltip 经 fmt 显示「—」）。 */
function renderScalar(rec, payload){
  var p = rec.placement;
  var o = {
    span: 'c' + (p.span || 3),
    title: p.title || p.card,
    tag: payload.date || payload.as_of || null,
    value: payload.value,
    unit: payload.unit,
    compare_label: payload.compare_label
  };
  var subs = [];
  if (payload.delta_pct != null) o.delta = payload.delta_pct * 100;
  if (payload.target != null){
    var rate = payload.rate != null ? payload.rate
      : (payload.target ? payload.value / payload.target : null);
    subs.push(miniBar(rate != null && isFinite(rate) ? rate : 0) +
      '&nbsp;&nbsp;目标 ' + wan(payload.target) + ' 万 · 达成 ' + pct(rate));
  }
  /* 年度达成卡分线明细：找不到数据的线标红为 0（「未接入」不是
   * 「0 销量」）；红色复用 --severity-p0，不新造色值。 */
  if (payload.lines && payload.lines.length){
    var lineParts = payload.lines.map(function(ln){
      if (ln.has_data === false){
        return '<span style="color:var(--severity-p0);font-weight:600">' +
          esc(ln.name) + ' ' + wan(ln.value) + ' 万（未接入）</span>';
      }
      return esc(ln.name) + ' ' + wan(ln.value) + ' 万';
    });
    subs.push(lineParts.join(' &nbsp;·&nbsp; '));
  }
  var refv = payload.latest !== undefined ? payload.latest : payload.prev;
  if (refv != null){
    var refl = payload.compare_label || (payload.latest !== undefined ? '昨日' : '前一日');
    subs.push(esc(refl) + ' ' + (payload.unit === '元' ? wan(refv) + ' 万' : nf(refv)));
  }
  if (subs.length) o.sub = subs.join(' &nbsp;·&nbsp; ');
  var w = CardKpi(o);
  replaceWidget(rec, w);
  if (payload.trend7 && payload.trend7.length){
    var body = w.querySelector('.card-body');
    var host = el('div');
    host.style.height = '44px';
    body.appendChild(host);
    ChartLine(host, {
      cats: payload.trend7.map(function(pt){ return String(pt.date).slice(5); }),
      series: [{name: '', data: payload.trend7.map(function(pt){ return pt.value; })}],
      height: 44,
      fmt: moneyFmt(payload.unit)
    });
  }
}

function renderLine(rec, payload){
  var p = rec.placement;
  var dates = payload.dates || [], series = payload.series || [];
  if (!dates.length || !series.length){ renderEmptyCard(rec, EMPTY_TEXT); return; }
  replaceWidget(rec, CardLine({
    span: 'c' + (p.span || 8),
    title: p.title || p.card,
    cats: dates,
    series: series,
    status: payload.status,
    fmt: moneyFmt(payload.unit)
  }));
  runDrawers();
}

function renderBar(rec, payload){
  var p = rec.placement;
  var cats = payload.categories || [], values = payload.values || [];
  if (!cats.length){ renderEmptyCard(rec, EMPTY_TEXT); return; }
  replaceWidget(rec, CardBar({
    span: 'c' + (p.span || 6),
    title: p.title || p.card,
    cats: cats,
    series: [{name: p.title || p.card, data: values}],
    fmt: moneyFmt(payload.unit)
  }));
  runDrawers();
}

function renderPie(rec, payload){
  var p = rec.placement;
  var items = payload.items || [];
  if (!items.length){ renderEmptyCard(rec, EMPTY_TEXT); return; }
  replaceWidget(rec, CardDonut({
    span: 'c' + (p.span || 6),
    title: p.title || p.card,
    items: items,
    fmt: moneyFmt(payload.unit)
  }));
  runDrawers();
}

/* columns[].format → 渲染：wan→wan()、percent→pct()、pct→带符号百分数、
 * delta→deltaHtml（比率×100）、ratio→两位小数、number→nf()、severity→chip；
 * 无 format → esc 原文；null 一律「—」。 */
function mapColumn(c){
  var col = {key: c.key, label: c.title};
  var f = c.format;
  if (f === 'severity'){
    col.chip = true;
    col.chipMap = CHIP_MAP;
  } else if (f === 'wan'){
    col.render = function(v){ return v == null ? PLACEHOLDER : wan(v); };
  } else if (f === 'percent'){
    col.render = function(v){ return v == null ? PLACEHOLDER : pct(v); };
  } else if (f === 'pct'){
    col.render = function(v){
      return v == null || !isFinite(v) ? PLACEHOLDER
        : (v > 0 ? '+' : '') + Number(v).toFixed(1) + '%';
    };
  } else if (f === 'delta'){
    col.render = function(v){
      return v == null || !isFinite(v) ? PLACEHOLDER : deltaHtml(v * 100);
    };
  } else if (f === 'ratio'){
    col.render = function(v){ return v == null ? PLACEHOLDER : Number(v).toFixed(2); };
  } else if (f === 'number'){
    col.render = function(v){ return v == null ? PLACEHOLDER : nf(v); };
  } else {
    col.render = function(v){ return v == null ? PLACEHOLDER : esc(v); };
  }
  return col;
}

function renderTable(rec, payload){
  var p = rec.placement;
  if (payload.has_fact === false){
    replaceWidget(rec, CardHtml('c' + (p.span || 12), 'h-chart',
      p.title || p.card, null, '<div class="empty">' + esc(NO_FACT_TEXT) + '</div>'));
    return;
  }
  replaceWidget(rec, CardTable({
    span: 'c' + (p.span || 12),
    title: p.title || p.card,
    tag: payload.as_of || null,
    cols: (payload.columns || []).map(mapColumn),
    rows: payload.rows || [],
    foot: false
  }));
}

function renderCard(rec, payload){
  if (!payload || !payload.chart){ showCardError(rec); return; }
  if (payload.chart === 'scalar') renderScalar(rec, payload);
  else if (payload.chart === 'line') renderLine(rec, payload);
  else if (payload.chart === 'bar') renderBar(rec, payload);
  else if (payload.chart === 'pie') renderPie(rec, payload);
  else if (payload.chart === 'table') renderTable(rec, payload);
  else { showCardError(rec); return; }
  attachGranSeg(rec);
  noteAsOf(rec, payload);
}

/* ---- 拖拽（事件委托 #grid，批次 B §5） ---------------------------------------
 * 监听绑 #grid 不绑 widget——replaceWidget() 整块替换节点，绑 widget 重渲染
 * 即失效。draggable 只挂 .widget__drag 把手 ⇒ dragstart 只在把手上发起，
 * 卡级 Seg / 折叠钮的点击不会被吞。拖拽只改 DOM 顺序 + localStorage，
 * 绝不触发 loadCard。 */
var dragWidget = null;
var phEl = null;

function upTo(node, cls, stop){
  while (node && node !== stop && node !== document){
    if (node.classList && node.classList.contains(cls)) return node;
    node = node.parentNode;
  }
  return null;
}

function cleanupDrag(){
  if (phEl && phEl.parentNode) phEl.parentNode.removeChild(phEl);
  phEl = null;
  dragWidget = null;
}

function bindLayoutDnd(){
  var grid = document.getElementById('grid');
  grid.addEventListener('dragstart', function(e){
    var handle = upTo(e.target, 'widget__drag', grid);
    var w = handle && upTo(handle, 'widget', grid);
    if (!w){ e.preventDefault(); return; }
    dragWidget = w;
    /* 虚线占位：与 dragged widget 同栅格跨度、同高度，drop 时以占位定锚点。 */
    var span = (w.className.match(/\bc\d+\b/) || ['c12'])[0];
    phEl = el('div', 'widget--placeholder ' + span);
    phEl.style.minHeight = w.offsetHeight + 'px';
    if (e.dataTransfer){
      e.dataTransfer.effectAllowed = 'move';
      try{ e.dataTransfer.setData('text/plain', w.getAttribute('data-card') || ''); }catch(err){}
      try{ e.dataTransfer.setDragImage(w, 24, 16); }catch(err){}
    }
  });
  grid.addEventListener('dragover', function(e){
    if (!dragWidget || !phEl) return;
    e.preventDefault();
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    var w = upTo(e.target, 'widget', grid);
    if (!w || w === dragWidget) return;
    var r = w.getBoundingClientRect();
    var before = (e.clientY - r.top) < r.height / 2;
    grid.insertBefore(phEl, before ? w : w.nextSibling);
  });
  grid.addEventListener('drop', function(e){
    if (!dragWidget) return;
    e.preventDefault();
    if (phEl && phEl.parentNode === grid){
      grid.insertBefore(dragWidget, phEl);
      cleanupDrag();
      persistLayout();
    } else {
      cleanupDrag();
    }
  });
  grid.addEventListener('dragend', function(){ cleanupDrag(); });

  /* 触屏兜底（HTML5 DnD 在 iPad 不生效）：把手点击弹「上移/下移/置顶」
   * 小菜单；折叠钮点击切折叠。 */
  grid.addEventListener('click', function(e){
    var col = upTo(e.target, 'widget__collapse', grid);
    if (col){
      var cw = upTo(col, 'widget', grid);
      if (cw) toggleCollapse(cw);
      return;
    }
    var handle = upTo(e.target, 'widget__drag', grid);
    if (handle) openMoveMenu(upTo(handle, 'widget', grid), e.clientX, e.clientY);
  });
}

/* 移动菜单：复用既有 .btn，容器样式全 inline（不加新类名/色值）。 */
var moveMenu = null;
function closeMoveMenu(){
  if (moveMenu && moveMenu.parentNode) moveMenu.parentNode.removeChild(moveMenu);
  moveMenu = null;
}
document.addEventListener('click', function(e){
  if (!moveMenu) return;
  if (moveMenu.contains(e.target)) return;           /* 菜单内点击由按钮自理 */
  if (upTo(e.target, 'widget__drag', null)) return;  /* 开菜单的那次点击 */
  closeMoveMenu();
});

function openMoveMenu(widget, x, y){
  if (!widget) return;
  closeMoveMenu();
  var grid = document.getElementById('grid');
  var m = el('div');
  m.style.cssText = 'position:fixed;z-index:130;display:flex;flex-direction:column;gap:4px;' +
    'background:var(--card-bg);border:1px solid var(--card-border);' +
    'border-radius:var(--radius-sm);box-shadow:var(--shadow-lg);padding:6px';
  var acts = [
    ['上移', function(){
      var prev = widget.previousElementSibling;
      if (prev) grid.insertBefore(widget, prev);
    }],
    ['下移', function(){
      var next = widget.nextElementSibling;
      if (next) grid.insertBefore(widget, next.nextElementSibling);
    }],
    ['置顶', function(){
      if (grid.firstElementChild !== widget) grid.insertBefore(widget, grid.firstElementChild);
    }]
  ];
  acts.forEach(function(a){
    var b = el('button', 'btn', esc(a[0]));
    b.type = 'button';
    b.style.cssText = 'height:26px;padding:0 12px;font-size:12px;white-space:nowrap';
    b.addEventListener('click', function(){
      a[1]();
      persistLayout();
      closeMoveMenu();
    });
    m.appendChild(b);
  });
  document.body.appendChild(m);
  var left = Math.max(8, Math.min(x, window.innerWidth - m.offsetWidth - 8));
  var top = Math.max(8, Math.min(y, window.innerHeight - m.offsetHeight - 8));
  m.style.left = left + 'px';
  m.style.top = top + 'px';
  moveMenu = m;
}

/* ---- 启动管线 -------------------------------------------------------------- */

function showPageError(){
  var grid = document.getElementById('grid');
  clear(grid);
  grid.appendChild(CardHtml('c12', '', '加载失败', null,
    '<div class="empty">' + esc(ERROR_TEXT) + '</div>'));
  toast(ERROR_TEXT);
}

function initShell(){
  var dashId = dashboardId();
  currentDashId = dashId;
  bindLayoutDnd();
  fetchJson('/api/v1/dashboards').then(function(payload){
    buildNav(payload.dashboards || [], dashId);
  }).catch(function(){}); /* 导航失败不阻塞页面 */
  fetchJson('/api/v1/dashboards/' + encodeURIComponent(dashId)).then(function(def){
    if (def.title) document.title = def.title;
    lastDef = def;
    renderHead(def);
    return buildFilters(def).then(function(){ return def; });
  }).then(function(def){
    buildCards(def);
    records.forEach(function(rec){ loadCard(dashId, rec); });
    var seconds = isFinite(def.refresh_seconds) ? def.refresh_seconds
      : DEFAULT_REFRESH_SECONDS;
    if (seconds > 0){
      setInterval(function(){
        records.forEach(function(rec){ loadCard(dashId, rec); });
      }, seconds * 1000);
    }
  }).catch(function(){
    showPageError();
  });
}

if (document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', initShell);
} else {
  initShell();
}
