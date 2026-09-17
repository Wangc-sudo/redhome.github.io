/* bi-data.js 数据适配层（2026-09-17 原稿转正）：原稿壳（bi.js/bi.html）+
 * /api/v1/* 驱动渲染。语义照抄 legacy web/dashboard.js：
 * pathname 取看板 id → dashboards 导航 → definition 页头/筛选 → 逐卡取数
 * （cache:"no-store"，WeakMap 竞态防护），payload.chart 分派到原稿卡片
 * 构造器；400/非 200 → 卡内错误态，整页不挂。零依赖，ES5。
 * 契约（bi.js 提供）：esc nf wan pct money moneyUnit deltaHtml el clear
 * toast CardBox CardKpi CardLine CardBar CardDonut CardTable CardHtml
 * ChartLine miniBar runDrawers PAL；挂载点 id：nav pgTitle pgSub pgNote
 * pgFilters grid footAsOf tip toast。
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

function renderHead(def){
  document.getElementById('pgTitle').textContent = def.title || '';
  document.getElementById('pgSub').textContent =
    (def.cards || []).length + ' 张卡片 · 数据口径 T+1';
  document.getElementById('pgNote').textContent = refreshNote(def.refresh_seconds);
}

/* 一个筛选下拉：label + select（首项「全部」= 空值），选项来自
 * /api/v1/options/{source}；变更只重拉 params 声明过该 param 的卡。 */
function buildFilters(def){
  var bar = document.getElementById('pgFilters');
  clear(bar);
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
          if (ps.indexOf(spec.param) >= 0) loadCard(rec);
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
    box.body.innerHTML = '<div class="empty">' + esc(LOADING_TEXT) + '</div>';
    grid.appendChild(box.widget);
    return {placement: placement, index: index, widget: box.widget};
  });
}

/* 某卡实际要带的参数：当前筛选值 ∩ 该卡 params 白名单；空值不传。 */
function cardUrl(dashId, rec){
  var base = '/api/v1/d/' + encodeURIComponent(dashId) +
    '/cards/' + encodeURIComponent(rec.placement.card);
  var allowed = rec.placement.params || [];
  var qs = [];
  allowed.forEach(function(name){
    var v = currentFilters[name];
    if (v != null && v !== '') qs.push(encodeURIComponent(name) + '=' + encodeURIComponent(v));
  });
  return qs.length ? base + '?' + qs.join('&') : base;
}

function loadCard(dashId, rec){
  var token = (loadToken.get(rec) || 0) + 1;
  loadToken.set(rec, token);
  fetchJson(cardUrl(dashId, rec)).then(function(payload){
    if (loadToken.get(rec) !== token) return;
    renderCard(rec, payload);
  }).catch(function(){
    if (loadToken.get(rec) !== token) return;
    showCardError(rec);
  });
}

function replaceWidget(rec, widget){
  rec.widget.parentNode.replaceChild(widget, rec.widget);
  rec.widget = widget;
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
    unit: payload.unit
  };
  var subs = [];
  if (payload.delta_pct != null) o.delta = payload.delta_pct * 100;
  if (payload.target != null){
    var rate = payload.rate != null ? payload.rate
      : (payload.target ? payload.value / payload.target : null);
    subs.push(miniBar(rate != null && isFinite(rate) ? rate : 0) +
      '&nbsp;&nbsp;目标 ' + wan(payload.target) + ' 万 · 达成 ' + pct(rate));
  }
  var refv = payload.latest !== undefined ? payload.latest : payload.prev;
  if (refv != null){
    var refl = payload.latest !== undefined ? '昨日' : '前一日';
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
  noteAsOf(rec, payload);
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
  fetchJson('/api/v1/dashboards').then(function(payload){
    buildNav(payload.dashboards || [], dashId);
  }).catch(function(){}); /* 导航失败不阻塞页面 */
  fetchJson('/api/v1/dashboards/' + encodeURIComponent(dashId)).then(function(def){
    if (def.title) document.title = def.title;
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
