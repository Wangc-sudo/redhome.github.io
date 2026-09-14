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
 *     「前一日 X · 环比 ±X%」（升绿降红）+ 卡内迷你趋势（缺数日断线）。
 *   - table 渲染：columns[].format ∈ wan/percent/ratio，null → 「—」，
 *     空结果显示「暂无数据」占位行。
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
   * 转百分比（1 位小数）；ratio → 2 位小数；缺省 → 原样字符串。 */
  const formatCell = (value, format) => {
    if (value === null || value === undefined) return PLACEHOLDER;
    if (format === "wan") return formatWan(value);
    if (format === "percent") return (value * 100).toFixed(1) + "%";
    if (format === "ratio") return Number(value).toFixed(2);
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

  /* 日环比次行：前一日 X · 环比 ±X%（升绿降红）· 数据日，下挂 7 日
   * 迷你趋势（connectNulls:false——缺数日断线，null = 无数）。 */
  const renderDodSub = (body, payload) => {
    const sub = document.createElement("div");
    sub.className = "kpi-sub";

    const prevText = payload.prev === null || payload.prev === undefined
      ? PLACEHOLDER
      : formatWan(payload.prev);
    const prev = document.createElement("span");
    prev.textContent = `前一日 ${prevText}`;
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

  /* ---- 表格 ---------------------------------------------------------------- */

  const renderTable = (body, payload) => {
    disposeChart(body);
    body.textContent = "";

    const columns = payload.columns || [];
    const table = document.createElement("table");
    table.className = "data-table";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    columns.forEach((column) => {
      const th = document.createElement("th");
      th.textContent = column.title;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    if (payload.rows && payload.rows.length) {
      payload.rows.forEach((row) => {
        const tr = document.createElement("tr");
        columns.forEach((column) => {
          const td = document.createElement("td");
          td.textContent = formatCell(row[column.key], column.format);
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
    } else {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.className = "empty-cell";
      td.colSpan = columns.length;
      td.textContent = EMPTY_TEXT;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    body.appendChild(table);
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
    if (payload && payload.chart === "table") return renderTable(body, payload);
    showError(body); // 未知 chart 类型（前端只实现 scalar/line/bar/table）
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
        buildCards(currentId, definition.cards || []);
        wireFilters();
        syncSelects();
        const cards = Array.from(document.querySelectorAll(".card"));
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
