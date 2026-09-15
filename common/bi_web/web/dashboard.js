/* bi-web 看板前端（API-first，2026-09-14 前后端分离规格）：原生 JS，
 * 无框架、无构建。页面骨架（导航/筛选/卡片）全部由客户端按
 * /api/v1/ 动态构建，服务端只返回数据 JSON 与这份静态壳：
 *
 *   - 启动：location.pathname 解析看板 id → GET /api/v1/dashboards
 *     构建顶部导航 → GET /api/v1/dashboards/{id} 取看板定义 →
 *     按 filters 并行 GET /api/v1/options/{source} 构建下拉 → 按
 *     cards 构建占位卡（data-api=/api/v1/d/{id}/cards/{card}）。
 *   - 每张 .card 按 data-api 拉取（no-store），按 payload.chart 分派
 *     渲染，拉取/渲染失败一律展示 .error「加载失败」；轮询间隔取看板
 *     定义的 refresh_seconds（缺省 300 秒），窗口 resize 同步图表。
 *     字段口径见 common/bi_web/queries.py。
 *   - 筛选下拉（.filters select[data-param]）变更 → URL replaceState
 *     不刷页 → 全部卡片带参重拉；参数按卡各自 data-params 白名单做
 *     交集，页面级参数不会 400 未声明它的兄弟卡；轮询沿用当前 URL。
 *   - scalar 按 unit 分派格式化：元 → 万；人 → 原样千分位。
 *   - 日环比 scalar 扩展字段 date/prev/delta_pct/trend7：次行
 *     「昨日 X · 环比 ±X%」（升绿降红）+ 卡内迷你趋势（缺数日断线）；
 *     主值是当日销售额的卡没有 latest，此时仍显示「前一日」。
 *   - 页头：看板标题 + 统计范围（当前筛选值，未选即「全部」）+ 数据
 *     截至（各卡 payload 的 as_of/date 最大值）与页面读取时间——两者
 *     分开显示，避免把刷新时刻误读成数据进度。
 *   - table 渲染：columns[].format ∈ wan/percent/ratio/number/delta，
 *     null → 「—」，空结果显示「暂无数据」占位行；表体自带工具条
 *     （数据截至 + 搜索）、可点表头排序（数值/中文，空值沉底）、
 *     固定表头与合计行，排序与搜索词随卡体存活（轮询重渲染不丢）。
 *   - pie 渲染：空心环形 + 右侧纵向图例（长名截断）；悬停 tooltip
 *     显示名称/销售额(万)/占比；头部 SKU 单列、长尾「其他」由服务端
 *     聚合；空数据显示「暂无数据」。
 *   - data-onclick-param 的 bar 卡：点击系列 → 设该参数（同步下拉）→
 *     replaceState → 全卡重拉。
 *   - 页面级失败（定义 401/404/503 或筛选选项拉取失败，对齐旧服务端
 *     渲染的 503 语义）→ 整页 .page-error「加载失败」，不构建卡片。
 *
 * ECharts 由 index.html 在浏览器侧从 CDN 加载，服务端零出网；全局
 * echarts 只在数据回调里触碰（CDN 未就绪时页面安静降级为错误态）。
 */
(() => {
  "use strict";

  const DEFAULT_REFRESH_SECONDS = 300;
  const ERROR_TEXT = "加载失败";
  const EMPTY_TEXT = "暂无数据";
  const PLACEHOLDER = "—";
  const ALL_TEXT = "全部";

  /* 当前看板定义：页头的统计范围由 filters + 当前 URL 参数推导。 */
  let currentDefinition = null;

  /* ---- 单位感知格式化 ---------------------------------------------------- */

  /* 元 → 万，中文千分位（GM 口径：金额一律以「万」展示）。 */
  const formatWan = (value) => (value / 10000).toLocaleString("zh-CN") + " 万";

  /* 人 → 原样千分位（不带「万」）。 */
  const formatCount = (value) => value.toLocaleString("zh-CN");

  /* scalar 大数字按 payload.unit 分派；未知单位退回原样字符串。
   * 主数值可为 null（最新日无数据的边角）——展示「—」而非「NaN 万」。 */
  const formatScalarValue = (payload) => {
    if (payload.value === null || payload.value === undefined) return PLACEHOLDER;
    if (payload.unit === "元") return formatWan(payload.value);
    if (payload.unit === "人") return formatCount(payload.value);
    return String(payload.value);
  };

  /* 表格单元格：null/undefined → 「—」；wan → 元转万；percent → 比率
   * 转百分比（1 位小数）；ratio → 2 位小数；number → 千分位整数；
   * delta → 环比（±x.x%，正绿负红）；缺省 → 原样字符串。 */
  const formatCell = (value, format) => {
    if (value === null || value === undefined) return PLACEHOLDER;
    if (format === "wan") return formatWan(value);
    if (format === "percent") return (value * 100).toFixed(1) + "%";
    if (format === "ratio") return Number(value).toFixed(2);
    if (format === "number") return Number(value).toLocaleString("zh-CN");
    if (format === "delta") return deltaText(value);
    return String(value);
  };



  /* 达成率 → 进度条百分比；rate 为 null（目标为 0）时返回 null，调用方展示「—」。 */
  const progressPercent = (rate) => {
    if (rate === null || rate === undefined || !isFinite(rate)) return null;
    return Math.min(100, Math.round(rate * 100));
  };

  /* 环比 → {text, up, down}；缺前值/前值为 0（delta_pct=null）→ null。 */
  const deltaPercent = (deltaPct) => {
    if (deltaPct === null || deltaPct === undefined || !isFinite(deltaPct)) {
      return null;
    }
    const rounded = Math.round(deltaPct * 1000) / 10;
    const sign = rounded > 0 ? "+" : "";
    return {
      text: `${sign}${rounded.toFixed(1)}%`,
      up: rounded > 0,
      down: rounded < 0,
    };
  };

  /* 表格里的环比：文本与配色沿用 deltaPercent 的取整/符号规则，
   * 与 KPI 卡的升绿降红保持一致（null → 「—」且不着色）。 */
  const deltaText = (value) => {
    const delta = deltaPercent(value);
    return delta === null ? PLACEHOLDER : delta.text;
  };

  const deltaClass = (value) => {
    const delta = deltaPercent(value);
    if (delta === null) return "flat";
    return delta.up ? "up" : delta.down ? "down" : "flat";
  };

  /* ---- echarts 封装 ------------------------------------------------------- */

  /* 返回（或首次创建）该卡体的图表实例。必须先挂 .chart 再 init：
   * echarts 在 init() 时快照容器尺寸，空 .card-body 只有 min-height
   * 140px；先加类、随后 init 内部读取 clientWidth/clientHeight 会触发
   * 同步回流，快照到的才是 .chart 的 280px（否则图表永远半高）。 */
  const ensureChart = (body) => {
    body.classList.add("chart");
    return echarts.getInstanceByDom(body) || echarts.init(body);
  };

  /* 卡体上若挂着图表实例则释放（scalar / table / 错误态复用同一卡体）。
   * 卡体内 .trend7 迷你趋势的实例一并释放：echarts 实例注册表对 DOM
   * 强引用，轮询每次重建 .trend7 节点而不 dispose 的话实例会无限累积。 */
  const disposeChart = (body) => {
    body.querySelectorAll(".trend7").forEach((node) => {
      const chart = window.echarts && window.echarts.getInstanceByDom(node);
      if (chart) chart.dispose();
    });
    if (body.classList.contains("chart")) {
      const chart = window.echarts && window.echarts.getInstanceByDom(body);
      if (chart) chart.dispose();
      body.classList.remove("chart");
    }
  };

  /* ---- 标量 KPI ---------------------------------------------------------- */

  /* 日环比次行：昨日 X · 环比 ±X%（升绿降红）· 数据日，下挂 7 日
   * 迷你趋势（connectNulls:false——缺数日断线，null = 无数）。
   * 主值是当日销售额的卡（线下/渠道日环比）没有 latest，此时退回显示
   * 前一日；商品动销卡主值是月累计，故显式给出「昨日」销售额。 */
  const renderDodSub = (body, payload) => {
    const sub = document.createElement("div");
    sub.className = "kpi-sub";

    const reference = payload.latest === undefined ? payload.prev : payload.latest;
    const label = payload.latest === undefined ? "前一日" : "昨日";
    const prevText = reference === null || reference === undefined
      ? PLACEHOLDER
      : formatWan(reference);
    const prev = document.createElement("span");
    prev.textContent = `${label} ${prevText}`;
    sub.appendChild(prev);

    const delta = deltaPercent(payload.delta_pct);
    if (delta !== null) {
      const arrow = document.createElement("span");
      arrow.className = `delta${delta.up ? " up" : delta.down ? " down" : ""}`;
      arrow.textContent = `环比 ${delta.text}`;
      sub.appendChild(arrow);
    }

    if (payload.date) {
      const date = document.createElement("span");
      date.className = "kpi-date";
      date.textContent = payload.date;
      sub.appendChild(date);
    }
    body.appendChild(sub);

    if (Array.isArray(payload.trend7) && payload.trend7.length) {
      const trend = document.createElement("div");
      trend.className = "trend7";
      body.appendChild(trend);
      /* 同 ensureChart 的快照约束：.trend7 高度由 CSS 先定死再 init。 */
      const chart = echarts.getInstanceByDom(trend) || echarts.init(trend);
      chart.setOption(
        {
          grid: { left: 8, right: 8, top: 8, bottom: 8 },
          xAxis: {
            type: "category",
            show: false,
            data: payload.trend7.map((point) => point.date),
          },
          yAxis: { type: "value", show: false },
          series: [
            {
              type: "line",
              showSymbol: false,
              connectNulls: false,
              data: payload.trend7.map((point) =>
                point.value === null ? null : point.value
              ),
            },
          ],
        },
        true
      );
    }
  };

  const renderScalar = (body, payload) => {
    disposeChart(body);
    body.textContent = "";
    body.classList.add("kpi"); // 紧凑保底高度（见 style.css .card-body.kpi）

    const value = document.createElement("div");
    value.className = "kpi-value";
    value.textContent = formatScalarValue(payload);
    body.appendChild(value);

    if (payload.date !== undefined) {
      renderDodSub(body, payload);
      return;
    }

    if (payload.target === undefined || payload.target === null) return;

    const percent = progressPercent(payload.rate);
    const sub = document.createElement("div");
    sub.className = "kpi-sub";
    sub.textContent = percent === null
      ? `目标 ${formatWan(payload.target)} · 达成 ${PLACEHOLDER}`
      : `目标 ${formatWan(payload.target)} · 达成 ${percent}%`;
    body.appendChild(sub);

    const track = document.createElement("div");
    track.className = "progress";
    const bar = document.createElement("div");
    bar.className = "progress-bar";
    bar.style.width = `${percent === null ? 0 : percent}%`;
    track.appendChild(bar);
    body.appendChild(track);
  };

  /* ---- 折线 / 柱状 -------------------------------------------------------- */

  const renderLine = (body, payload) => {
    ensureChart(body).setOption({
      tooltip: { trigger: "axis", valueFormatter: formatWan },
      legend: { top: 0 },
      grid: { left: 8, right: 16, top: 36, bottom: 8, containLabel: true },
      xAxis: { type: "category", boundaryGap: false, data: payload.dates },
      yAxis: { type: "value", axisLabel: { formatter: formatWan } },
      series: payload.series.map((entry) => ({
        name: entry.name,
        type: "line",
        showSymbol: false,
        data: entry.data,
      })),
    }, true);
  };

  /* bar 点击下钻：设参数（同步下拉）→ replaceState → 全卡重拉。 */
  const wireDrill = (card, body) => {
    const param = card.dataset.onclickParam;
    if (!param) return;
    const chart = echarts.getInstanceByDom(body);
    if (!chart) return;
    chart.off("click"); /* 轮询会复用实例，先解绑避免 handler 叠加 */
    chart.on("click", (event) => {
      if (event && event.name) setParam(param, String(event.name));
    });
  };

  const renderBar = (card, body, payload) => {
    ensureChart(body).setOption({
      tooltip: { valueFormatter: formatWan },
      grid: { left: 8, right: 24, top: 8, bottom: 8, containLabel: true },
      // categories 已按金额降序返回；inverse: true 让第一名排在最上方。
      xAxis: { type: "value", axisLabel: { formatter: formatWan } },
      yAxis: { type: "category", inverse: true, data: payload.categories },
      series: [{ type: "bar", barMaxWidth: 28, data: payload.values }],
    }, true);
    wireDrill(card, body);
  };

  /* ---- 环形图（SKU 销售占比） ---------------------------------------------- */

  /* 空心环形：右侧纵向图例承载切片名（长名截断，悬停 tooltip 有全名），
   * tooltip 显示名称/销售额(万)/占比——占比用 ECharts 内建 percent
   *（分母即 items 合计，与服务端口径一致）。 */
  const renderPie = (body, payload) => {
    const items = payload.items || [];
    if (!items.length) {
      disposeChart(body);
      body.textContent = "";
      const empty = document.createElement("div");
      empty.className = "empty-cell";
      empty.textContent = EMPTY_TEXT;
      body.appendChild(empty);
      return;
    }
    ensureChart(body).setOption({
      tooltip: {
        trigger: "item",
        formatter: (params) =>
          `${params.marker} ${params.name}<br/>销售额 ${formatWan(params.value)} · 占比 ${Number(params.percent).toFixed(1)}%`,
      },
      legend: {
        orient: "vertical",
        right: 8,
        top: "middle",
        formatter: (name) => (name.length > 12 ? `${name.slice(0, 12)}…` : name),
      },
      series: [
        {
          type: "pie",
          radius: ["42%", "68%"],
          center: ["38%", "50%"],
          label: { show: false },
          itemStyle: { borderColor: "#ffffff", borderWidth: 2 },
          data: items,
        },
      ],
    }, true);
  };

  /* ---- 表格 ---------------------------------------------------------------- */

  /* 每张表的交互状态（排序列/方向/搜索词）：随卡体存活，轮询重渲染后
   * 用户的排序与搜索词不丢。 */
  const tableState = new WeakMap();

  /* 排序：数值列比大小，文本列按中文排序；空值永远沉底。 */
  const sortRows = (rows, key, desc) => {
    if (!key) return rows;
    return [...rows].sort((left, right) => {
      const a = left[key];
      const b = right[key];
      if (a === null || a === undefined) return 1;
      if (b === null || b === undefined) return -1;
      const diff =
        typeof a === "number" && typeof b === "number"
          ? a - b
          : String(a).localeCompare(String(b), "zh-CN");
      return diff * (desc ? -1 : 1);
    });
  };

  /* 搜索：任意列命中即保留（大小写不敏感）。 */
  const filterRows = (rows, columns, query) => {
    const needle = (query || "").trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      columns.some((column) =>
        String(row[column.key] ?? "").toLowerCase().includes(needle)
      )
    );
  };

  const renderTable = (body, payload) => {
    disposeChart(body);
    body.textContent = "";

    const columns = payload.columns || [];
    const rows = payload.rows || [];
    const state = tableState.get(body) || { sort: null, desc: true, query: "" };
    tableState.set(body, state);
    if (payload.as_of) showAsOf(payload.as_of);

    /* 工具条：数据截至（来自 payload.as_of）+ 搜索框；与表格分开重建，
     * 输入时不重建工具条本身，避免输入框失焦。 */
    const tools = document.createElement("div");
    tools.className = "table-tools";
    const asOf = document.createElement("span");
    asOf.className = "table-asof";
    asOf.textContent = payload.as_of ? `数据截至 ${payload.as_of}` : "";
    const search = document.createElement("input");
    search.type = "search";
    search.className = "table-search";
    search.placeholder = "搜索";
    search.setAttribute("aria-label", "搜索表格内容");
    search.value = state.query;
    tools.append(asOf, search);
    body.appendChild(tools);

    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    const table = document.createElement("table");
    table.className = "data-table";
    const thead = document.createElement("thead");
    const tbody = document.createElement("tbody");
    const tfoot = document.createElement("tfoot");
    table.append(thead, tbody, tfoot);
    scroll.appendChild(table);
    body.appendChild(scroll);

    const paint = () => {
      const visible = sortRows(
        filterRows(rows, columns, state.query),
        state.sort,
        state.desc
      );

      thead.textContent = "";
      const headRow = document.createElement("tr");
      columns.forEach((column) => {
        const th = document.createElement("th");
        const button = document.createElement("button");
        button.type = "button";
        button.className = "th-sort";
        const active = state.sort === column.key;
        if (active) button.classList.add("active");
        button.textContent =
          column.title + (active ? (state.desc ? " ↓" : " ↑") : "");
        button.addEventListener("click", () => {
          state.desc = active ? !state.desc : true;
          state.sort = column.key;
          paint();
        });
        th.appendChild(button);
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);

      tbody.textContent = "";
      if (visible.length) {
        visible.forEach((row) => {
          const tr = document.createElement("tr");
          columns.forEach((column) => {
            const td = document.createElement("td");
            td.className = column.format === "delta" ? "delta-cell" : "";
            if (column.format === "delta") {
              td.classList.add(deltaClass(row[column.key]));
            }
            if (column.key === "goods") td.classList.add("text-cell");
            td.textContent = formatCell(row[column.key], column.format);
            tr.appendChild(td);
          });
          tbody.appendChild(tr);
        });
      } else {
        const tr = document.createElement("tr");
        const td = document.createElement("td");
        td.className = "empty-cell";
        td.colSpan = columns.length || 1;
        td.textContent = state.query ? "没有匹配记录" : EMPTY_TEXT;
        tr.appendChild(td);
        tbody.appendChild(tr);
      }

      /* 合计行：金额/数量列求和（null 计 0），始终贴在表尾可见。 */
      tfoot.textContent = "";
      if (visible.length) {
        const footRow = document.createElement("tr");
        columns.forEach((column, index) => {
          const td = document.createElement("td");
          if (index === 0) {
            td.textContent = `合计 ${visible.length} 条`;
          } else if (column.format === "wan" || column.format === "number") {
            const total = visible.reduce(
              (sum, row) => sum + (Number(row[column.key]) || 0),
              0
            );
            td.textContent = formatCell(total, column.format);
          }
          footRow.appendChild(td);
        });
        tfoot.appendChild(footRow);
      }
    };

    search.addEventListener("input", () => {
      state.query = search.value;
      paint();
    });
    paint();
  };

  /* ---- 分派与失败态 -------------------------------------------------------- */

  const showError = (body) => {
    disposeChart(body);
    body.textContent = "";
    const box = document.createElement("div");
    box.className = "error";
    box.textContent = ERROR_TEXT;
    body.appendChild(box);
  };

  const render = (card, body, payload) => {
    if (payload && payload.chart === "scalar") return renderScalar(body, payload);
    if (payload && payload.chart === "line") return renderLine(body, payload);
    if (payload && payload.chart === "bar") return renderBar(card, body, payload);
    if (payload && payload.chart === "pie") return renderPie(body, payload);
    if (payload && payload.chart === "table") return renderTable(body, payload);
    showError(body); // 未知 chart 类型（前端只实现 scalar/line/bar/pie/table）
  };

  /* ---- URL 参数与取数 ------------------------------------------------------ */

  /* 当前页 URL 参数（每次现取：轮询沿用筛选后的 URL）。 */
  const pageParams = () => new URLSearchParams(window.location.search);

  /* 某张卡实际要带的参数：页面参数 ∩ 该卡 data-params 白名单。 */
  const cardParams = (card) => {
    const allowed = (card.dataset.params || "").split(/\s+/).filter(Boolean);
    const current = pageParams();
    const params = new URLSearchParams();
    allowed.forEach((name) => {
      const value = current.get(name);
      if (value !== null) params.set(name, value);
    });
    return params;
  };

  const cardUrl = (card) => {
    const query = cardParams(card).toString();
    return query ? `${card.dataset.api}?${query}` : card.dataset.api;
  };

  /* 每卡的取数代次：慢到的旧响应（连续切筛选时）不得覆盖新响应。 */
  const loadToken = new WeakMap();

  const loadCard = (card) => {
    const body = card.querySelector(".card-body");
    const token = (loadToken.get(card) || 0) + 1;
    loadToken.set(card, token);
    fetch(cardUrl(card), { cache: "no-store" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        if (loadToken.get(card) === token) render(card, body, payload);
      })
      .catch(() => {
        if (loadToken.get(card) === token) showError(body);
      });
  };

  /* ---- 筛选下拉与 URL 同步 -------------------------------------------------- */

  /* URL → 下拉框选中项（空值 = 全部）。 */
  const syncSelects = () => {
    const current = pageParams();
    document.querySelectorAll(".filters select[data-param]").forEach((select) => {
      select.value = current.get(select.dataset.param) || "";
    });
  };

  /* 设/清一个参数 → replaceState 不刷页 → 全卡重拉 → 下拉同步。 */
  const setParam = (name, value) => {
    const params = pageParams();
    if (value === null || value === "") params.delete(name);
    else params.set(name, value);
    const query = params.toString();
    window.history.replaceState(
      null,
      "",
      query ? `?${query}` : window.location.pathname
    );
    document.querySelectorAll(".card").forEach(loadCard);
    syncSelects();
    renderScope();
  };

  const wireFilters = () => {
    document.querySelectorAll(".filters select[data-param]").forEach((select) => {
      select.addEventListener("change", () => {
        setParam(select.dataset.param, select.value);
      });
    });
  };

  /* ---- 页面骨架构建（API-first）-------------------------------------------- */

  /* 解析 /d/{dashboard_id}；服务端已把本壳限定在该路径下提供。 */
  const dashboardId = () => {
    const match = window.location.pathname.match(/^\/d\/([^/?#]+)/);
    return match ? decodeURIComponent(match[1]) : null;
  };

  const fetchJson = (url) =>
    fetch(url, { cache: "no-store" }).then((response) => {
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return response.json();
    });

  /* 页面级失败态：整行横跨的「加载失败」（对齐旧服务端渲染的 503 页）。 */
  const showPageError = () => {
    const main = document.getElementById("dashboard");
    main.textContent = "";
    const box = document.createElement("div");
    box.className = "page-error";
    box.textContent = ERROR_TEXT;
    main.appendChild(box);
  };

  /* 顶部导航：/api/v1/dashboards 列表 → 链接；当前页高亮。
   * 失败静默（无导航可用页，与旧服务端渲染的 fail-open 语义一致）。 */
  const buildNav = (currentId) =>
    fetchJson("/api/v1/dashboards")
      .then((payload) => {
        const entries = payload.dashboards || [];
        if (!entries.length) return;
        const nav = document.getElementById("topnav");
        entries.forEach((entry) => {
          const link = document.createElement("a");
          link.className =
            "topnav-link" + (entry.id === currentId ? " current" : "");
          link.href = `/d/${encodeURIComponent(entry.id)}`;
          link.textContent = entry.title || entry.id;
          nav.appendChild(link);
        });
        nav.hidden = false;
      })
      .catch(() => {});

  /* 一个筛选下拉：label + select（首项「全部」），选项来自
   * /api/v1/options/{source}。任一来源失败 → 页面级失败（对齐旧语义）。 */
  const buildFilters = (filters) => {
    if (!filters.length) return Promise.resolve();
    const container = document.getElementById("filters");
    return Promise.all(
      filters.map((spec) =>
        fetchJson(`/api/v1/options/${encodeURIComponent(spec.source)}`).then(
          (payload) => {
            const wrapper = document.createElement("div");
            wrapper.className = "filter";

            const label = document.createElement("span");
            label.className = "filter-label";
            label.textContent = spec.label || spec.param;
            wrapper.appendChild(label);

            const select = document.createElement("select");
            select.dataset.param = spec.param;
            const all = document.createElement("option");
            all.value = "";
            all.textContent = ALL_TEXT;
            select.appendChild(all);
            (payload.options || []).forEach((optionValue) => {
              const option = document.createElement("option");
              option.value = optionValue;
              option.textContent = optionValue;
              select.appendChild(option);
            });
            wrapper.appendChild(select);
            container.appendChild(wrapper);
          }
        )
      )
    ).then(() => {
      container.hidden = false;
    });
  };

  /* 一张占位卡：结构与旧模板一致（.card span-N + h2 + .card-body），
   * data-api 指向 v1 卡片别名，data-params 为该卡白名单。 */
  const buildCards = (currentId, cards) => {
    const main = document.getElementById("dashboard");
    cards.forEach((placement) => {
      const section = document.createElement("section");
      section.className = `card span-${placement.span}`;
      section.dataset.api = `/api/v1/d/${encodeURIComponent(currentId)}/cards/${encodeURIComponent(placement.card)}`;
      if (placement.params && placement.params.length) {
        section.dataset.params = placement.params.join(" ");
      }
      if (placement.on_click) {
        section.dataset.onclickParam = placement.on_click;
      }

      const title = document.createElement("h2");
      title.textContent = placement.title || placement.card;
      section.appendChild(title);

      const body = document.createElement("div");
      body.className = "card-body";
      section.appendChild(body);

      main.appendChild(section);
    });
  };

  /* ---- 页头：标题 / 统计范围 / 数据截至 · 读取时间 ------------------------- */

  /* 数据截至：取各卡 payload 的 as_of/date 最大值。与「读取时间」分开显示，
   * 避免把刷新时刻误当成数据进度（财务 Mart 空表时尤其要能看出差别）。 */
  let asOfSeen = "";
  const showAsOf = (value) => {
    if (!value) return;
    const text = String(value);
    if (text <= asOfSeen) return;
    asOfSeen = text;
    renderUpdated();
  };

  const renderUpdated = () => {
    const node = document.getElementById("page-updated");
    if (!node) return;
    const readAt = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    node.textContent = asOfSeen
      ? `数据截至 ${asOfSeen} · 读取于 ${readAt}`
      : `读取于 ${readAt}`;
  };

  /* 统计范围：当前生效的筛选值，未选即「全部」。 */
  const renderScope = () => {
    const node = document.getElementById("page-scope");
    if (!node || !currentDefinition) return;
    const params = pageParams();
    const parts = (currentDefinition.filters || []).map((spec) => {
      const value = params.get(spec.param);
      return `${spec.label || spec.param}：${value || ALL_TEXT}`;
    });
    node.textContent = parts.length ? parts.join(" · ") : "全部范围";
    renderUpdated();
  };

  const renderHead = (definition) => {
    currentDefinition = definition;
    document.getElementById("page-title").textContent = definition.title || "";
    document.getElementById("pagehead").hidden = false;
    renderScope();
  };

  /* ---- 启动与轮询 ---------------------------------------------------------- */

  /* 窗口尺寸变化（含加载后跨 900px 断点）时把在用图表实例同步到新画布
   * 尺寸——卡体图表与 .trend7 迷你趋势都要同步。echarts 实例不会自动
   * 跟随容器。CDN 未加载（无全局 echarts）或卡体暂无实例时静默跳过，
   * 绝不抛错。 */
  const resizeCharts = () => {
    document.querySelectorAll(".card-body.chart, .card-body .trend7").forEach((node) => {
      const chart = window.echarts && window.echarts.getInstanceByDom(node);
      if (chart) chart.resize();
    });
  };

  const start = () => {
    const currentId = dashboardId();
    if (!currentId) {
      showPageError(); // 壳只在 /d/{id} 下提供；防御未知路径直接打开。
      return;
    }
    buildNav(currentId);
    fetchJson(`/api/v1/dashboards/${encodeURIComponent(currentId)}`)
      .then((definition) =>
        buildFilters(definition.filters || []).then(() => definition)
      )
      .then((definition) => {
        if (definition.title) document.title = definition.title;
        renderHead(definition);
        buildCards(currentId, definition.cards || []);
        wireFilters();
        syncSelects();
        const cards = Array.from(document.querySelectorAll(".card"));
        /* 手动刷新：重拉全部卡片并刷新读取时间（轮询之外的即时入口）。 */
        document
          .getElementById("page-refresh")
          .addEventListener("click", () => {
            cards.forEach(loadCard);
            renderUpdated();
          });
        cards.forEach(loadCard);
        const seconds = Number.isFinite(definition.refresh_seconds)
          ? definition.refresh_seconds
          : DEFAULT_REFRESH_SECONDS;
        setInterval(() => cards.forEach(loadCard), seconds * 1000);
        window.addEventListener("resize", resizeCharts);
      })
      .catch(() => {
        showPageError();
      });
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
