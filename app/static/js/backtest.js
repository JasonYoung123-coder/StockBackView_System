const statusEl = document.getElementById("status");
const formEl = document.getElementById("backtest-form");
const strategySelectEl = document.getElementById("strategy-select");
const strategyParamsPanelEl = document.getElementById("strategy-params-panel");
const strategyParamsFieldsEl = document.getElementById("strategy-params-fields");
const strategyParamsHintEl = document.getElementById("strategy-params-hint");
const metricsEl = document.getElementById("metrics");
const signalSummaryEl = document.getElementById("signal-summary");
const tradeSummaryEl = document.getElementById("trade-summary");
const tradeCountEl = document.getElementById("trade-count");
const tradeRecordsBodyEl = document.getElementById("trade-records-body");
const progressContainerEl = document.getElementById("progress-container");
const progressMessageEl = document.getElementById("progress-message");
const progressPercentEl = document.getElementById("progress-percent");
const progressBarEl = document.getElementById("progress-bar");
const submitButton = document.getElementById("submit-button");
const chartEl = document.getElementById("chart");
const tradeTableEl = document.getElementById("trade-table");
const jobMetaWrapEl = document.getElementById("job-meta-wrap");
const dailyDetailPanelEl = document.getElementById("daily-detail-float");
const dailyDetailTitleEl = document.getElementById("daily-detail-title");
const dailyOverviewEl = document.getElementById("daily-overview");
const dailyHoldingsEl = document.getElementById("daily-holdings");
const dailyHoldingsCountEl = document.getElementById("daily-holdings-count");
const backtestPlaceholderEl = document.getElementById("backtest-placeholder");
const backtestLoadingEl = document.getElementById("backtest-loading");
const loadingTipEl = document.getElementById("loading-tip");
let chart = null;

let currentPollingTimer = null;
let strategyMap = new Map();
let currentTradeRecords = [];
let currentSortState = { key: null, direction: "asc" };
let currentJobId = "";
let currentJobResult = null;
let currentDailyPositionDetails = new Map();
let lastRenderedDetailDate = "";
let _lastMouseX = 0;
let _lastMouseY = 0;
let _tipTimer = null;

const BACKTEST_TIPS = [
  "💡 回测采用日频调仓，每个交易日收盘后生成次日目标组合权重",
  "📈 收益曲线的基准线为沪深300全收益指数（含分红再投资）",
  "🔍 回测完成后，将鼠标悬停在收益曲线上可查看每日持仓明细",
  "⚙️ 手续费与印花税会直接影响策略净收益，建议使用实际费率",
  "📊 最大回撤反映策略最坏情况下的亏损幅度，是风险评估的关键指标",
  "🔄 策略参数可在左侧面板调整，不同参数组合可能产生截然不同的结果",
  "📅 回测区间建议覆盖完整的牛熊周期，避免过拟合",
  "💰 夏普比率 > 1 通常被认为是不错的风险调整收益",
  "📉 关注逐笔交易记录中的「持有天数」，可帮助理解策略的换手节奏",
  "🎯 胜率不是唯一标准——低胜率但高盈亏比的策略也可以盈利",
];

const jobIdEl = document.createElement("span");
jobIdEl.className = "muted";
jobIdEl.textContent = "当前任务：暂无";

const exportJsonButton = document.createElement("button");
exportJsonButton.type = "button";
exportJsonButton.textContent = "导出本次结果 JSON";
exportJsonButton.disabled = true;
exportJsonButton.style.width = "auto";
exportJsonButton.style.minWidth = "180px";

if (jobMetaWrapEl) {
  jobMetaWrapEl.append(jobIdEl, exportJsonButton);
}

function updateJobMeta(jobId = "", jobResult = null) {
  currentJobId = jobId || "";
  currentJobResult = jobResult || null;
  jobIdEl.textContent = currentJobId ? `当前任务：${currentJobId}` : "当前任务：暂无";
  exportJsonButton.disabled = !(currentJobId && currentJobResult);
}

function exportCurrentJobResult() {
  if (!currentJobId || !currentJobResult) return;
  const payload = {
    job_id: currentJobId,
    exported_at: new Date().toISOString(),
    result: currentJobResult,
  };
  const blob = new Blob([JSON.stringify(payload, null, 2)], {
    type: "application/json;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `backtest_${currentJobId}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

exportJsonButton.addEventListener("click", exportCurrentJobResult);

async function loadStrategies() {
  statusEl.textContent = "正在加载策略列表...";
  try {
    const response = await fetch("/api/strategies");
    const strategies = await response.json();
    strategyMap = new Map(strategies.map((item) => [item.name, item]));

    strategySelectEl.innerHTML = "";
    strategies.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.name;
      option.textContent = item.adapted ? `${item.name}（已适配）` : item.name;
      strategySelectEl.appendChild(option);
    });

    if (strategies.length > 0) {
      renderStrategyParams(strategies[0]);
    }
    statusEl.textContent = `已加载 ${strategies.length} 个策略。`;
  } catch (error) {
    statusEl.textContent = `加载策略失败：${error.message}`;
  }
}

function formatPercent(value) {
  return `${(Number(value) * 100).toFixed(2)}%`;
}

function formatSignedPercent(value) {
  const numeric = Number(value);
  const prefix = numeric > 0 ? "+" : "";
  return `${prefix}${(numeric * 100).toFixed(2)}%`;
}

function formatMoney(value) {
  return Number(value).toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function setProgress(progress, message) {
  const safeProgress = Math.max(0, Math.min(100, Number(progress || 0)));
  progressContainerEl.classList.remove("hidden");
  progressBarEl.style.width = `${safeProgress}%`;
  progressPercentEl.textContent = `${safeProgress.toFixed(0)}%`;
  progressMessageEl.textContent = message || "正在回测...";
}

function hideProgress() {
  progressContainerEl.classList.add("hidden");
  hideLoadingTips();
}

function showLoadingTips() {
  if (!backtestLoadingEl || !loadingTipEl) return;
  if (backtestPlaceholderEl) backtestPlaceholderEl.classList.add("hidden");
  backtestLoadingEl.classList.remove("hidden");
  let idx = Math.floor(Math.random() * BACKTEST_TIPS.length);
  loadingTipEl.textContent = BACKTEST_TIPS[idx];
  _tipTimer = setInterval(() => {
    loadingTipEl.style.opacity = "0";
    setTimeout(() => {
      idx = (idx + 1) % BACKTEST_TIPS.length;
      loadingTipEl.textContent = BACKTEST_TIPS[idx];
      loadingTipEl.style.opacity = "1";
    }, 400);
  }, 5000);
}

function hideLoadingTips() {
  if (_tipTimer) { clearInterval(_tipTimer); _tipTimer = null; }
  if (backtestLoadingEl) backtestLoadingEl.classList.add("hidden");
}

function clearPreviousResult() {
  if (backtestPlaceholderEl) backtestPlaceholderEl.classList.add("hidden");
  if (chartEl) chartEl.classList.remove("hidden");

  metricsEl.innerHTML = "";
  signalSummaryEl.innerHTML = "";
  tradeSummaryEl.innerHTML = "";
  tradeCountEl.textContent = "暂无数据";
  currentDailyPositionDetails = new Map();
  currentTradeRecords = [];
  currentSortState = { key: null, direction: "asc" };
  clearSortIndicators();
  updateJobMeta();
  hideDailyDetail();
  tradeRecordsBodyEl.innerHTML = `
    <tr>
      <td colspan="12" class="muted empty-cell">回测完成后将在这里显示逐笔交易。</td>
    </tr>
  `;
  if (chart) {
    chart.clear();
  }
}

function renderStrategyParams(strategy) {
  const schema = strategy?.config_schema;
  if (!schema || !Array.isArray(schema.fields) || schema.fields.length === 0) {
    strategyParamsPanelEl.classList.add("hidden");
    strategyParamsFieldsEl.innerHTML = "";
    strategyParamsHintEl.textContent = "当前策略无额外参数";
    return;
  }

  strategyParamsPanelEl.classList.remove("hidden");
  strategyParamsHintEl.textContent = schema.title || "可调整当前策略参数";
  strategyParamsFieldsEl.innerHTML = schema.fields
    .map(
      (field) => `
        <label>
          ${field.label}
          <input
            name="strategy_param_${field.name}"
            data-strategy-param="${field.name}"
            type="${field.type || "number"}"
            value="${field.default ?? ""}"
            ${field.min !== undefined ? `min="${field.min}"` : ""}
            ${field.max !== undefined ? `max="${field.max}"` : ""}
            ${field.step !== undefined ? `step="${field.step}"` : ""}
          />
        </label>
      `
    )
    .join("");
}

function collectStrategyParams() {
  const params = {};
  document.querySelectorAll("[data-strategy-param]").forEach((input) => {
    const key = input.dataset.strategyParam;
    if (!key) return;
    const raw = input.value;
    params[key] = raw === "" ? raw : Number.isNaN(Number(raw)) ? raw : Number(raw);
  });
  return params;
}

function renderMetrics(metrics) {
  metricsEl.innerHTML = Object.entries(metrics)
    .map(
      ([name, item]) => {
        const cardClass = name === "策略收益" ? "strategy-card" : "benchmark-card";
        return `
        <div class="metric-card ${cardClass}">
          <h3>${name}</h3>
          <p>累计收益率：${formatPercent(item.total_return)}</p>
          <p>年化收益率：${formatPercent(item.annualized_return)}</p>
          <p>最大回撤：${formatPercent(item.max_drawdown)}</p>
          <p>波动率：${formatPercent(item.volatility)}</p>
          <p>夏普比率：${Number(item.sharpe_ratio).toFixed(2)}</p>
          <p>胜率：${formatPercent(item.win_rate)}</p>
          <p>期末资金：${formatMoney(item.final_value)}</p>
        </div>
      `;
      }
    )
    .join("");
}

function renderSignalSummary(summary) {
  const latestHoldings = (summary.latest_holdings || [])
    .map((item) => `${item.ts_code} ${item.name || ""}`.trim())
    .join("，");
  const warnings = Array.isArray(summary.warnings) ? summary.warnings : [];
  const warningHtml = warnings.length
    ? `
      <p>提示条数：${warnings.length}</p>
      <div style="margin-top:8px; padding-top:8px; border-top:1px dashed #cbd5e1;">
        <p style="margin-bottom:6px; color:#b45309;">策略提示：</p>
        ${warnings
          .slice(0, 8)
          .map((item) => `<p style="margin:4px 0; color:#92400e;">${item}</p>`)
          .join("")}
      </div>
    `
    : "";

  signalSummaryEl.innerHTML = `
    <div class="metric-card">
      <h3>信号摘要</h3>
      <p>买入次数：${summary.buy_signals}</p>
      <p>卖出次数：${summary.sell_signals}</p>
      <p>平均仓位：${formatPercent(summary.average_position)}</p>
      <p>平均持仓数：${Number(summary.average_holding_count || 0).toFixed(2)}</p>
      <p>调仓次数：${Number(summary.rebalance_count || 0).toFixed(0)}</p>
      <p>最新持仓：${latestHoldings || "无"}</p>
      ${warningHtml}
    </div>
  `;
}

function renderTradeSummary(summary) {
  tradeSummaryEl.innerHTML = `
    <div class="metric-card">
      <h3>交易汇总</h3>
      <p>总交易笔数：${summary.total_trades}</p>
      <p>盈利笔数：${summary.winning_trades}</p>
      <p>亏损笔数：${summary.losing_trades}</p>
      <p>交易胜率：${formatPercent(summary.win_rate)}</p>
      <p>总收益金额：${formatMoney(summary.total_pnl_amount)}</p>
      <p>平均单笔收益：${formatMoney(summary.average_pnl_amount)}</p>
      <p>平均单笔收益率：${formatPercent(summary.average_return_rate)}</p>
      <p>最佳单笔收益率：${formatPercent(summary.best_trade_return)}</p>
      <p>最差单笔收益率：${formatPercent(summary.worst_trade_return)}</p>
    </div>
  `;
}

function buildDailyPositionMap(details) {
  return new Map((details || []).map((item) => [item.date, item]));
}

function renderDailyDetail(date) {
  if (lastRenderedDetailDate === date) return;
  lastRenderedDetailDate = date;

  const detail = currentDailyPositionDetails.get(date);
  if (!detail || !dailyDetailPanelEl || !dailyOverviewEl || !dailyHoldingsEl || !dailyHoldingsCountEl) return;

  if (dailyDetailTitleEl) {
    dailyDetailTitleEl.textContent = `${detail.date} 持仓`;
  }

  const retClass = detail.daily_return >= 0 ? "profit" : "loss";
  dailyOverviewEl.innerHTML = `
    <span>日收益 <strong class="${retClass}">${formatSignedPercent(detail.daily_return)}</strong></span>
    <span class="float-sep">·</span>
    <span>资产 <strong>${formatMoney(detail.capital)}</strong></span>
    <span class="float-sep">·</span>
    <span>仓位 <strong>${formatPercent(detail.position)}</strong></span>
  `;

  const holdings = Array.isArray(detail.holdings) ? detail.holdings : [];
  dailyHoldingsCountEl.textContent = holdings.length ? `${holdings.length}只` : "";
  if (!holdings.length) {
    dailyHoldingsEl.innerHTML = `<div class="float-empty muted">当日无持仓</div>`;
  } else {
    dailyHoldingsEl.innerHTML = `
      <table class="float-holdings-table">
        <thead><tr>
          <th>股票</th><th>日收益</th><th>日收益额</th><th>总收益</th><th>浮盈额</th>
        </tr></thead>
        <tbody>
          ${holdings
            .map((item) => {
              const dc = item.daily_return >= 0 ? "profit" : "loss";
              const tc = item.total_return >= 0 ? "profit" : "loss";
              const dac = item.daily_pnl_amount >= 0 ? "profit" : "loss";
              const fac = item.floating_pnl_amount >= 0 ? "profit" : "loss";
              return `<tr>
                <td>${item.ts_code}<br/><span class="muted">${item.name || ""}</span></td>
                <td class="${dc}">${formatSignedPercent(item.daily_return)}</td>
                <td class="${dac}">${formatMoney(item.daily_pnl_amount)}</td>
                <td class="${tc}">${formatSignedPercent(item.total_return)}</td>
                <td class="${fac}">${formatMoney(item.floating_pnl_amount)}</td>
              </tr>`;
            })
            .join("")}
        </tbody>
      </table>
    `;
  }

  dailyDetailPanelEl.classList.remove("hidden");
  positionFloatingPanel(_lastMouseX, _lastMouseY);
}

function hideDailyDetail() {
  if (!dailyDetailPanelEl || !dailyOverviewEl || !dailyHoldingsEl || !dailyHoldingsCountEl) return;
  dailyDetailPanelEl.classList.add("hidden");
  dailyOverviewEl.innerHTML = "";
  dailyHoldingsEl.innerHTML = "";
  dailyHoldingsCountEl.textContent = "";
  lastRenderedDetailDate = "";
}

let _panelSide = "right";
let _panelLeft = 0;
let _panelWidth = 420;
let _panelTop = 0;

function positionFloatingPanel(mx, my) {
  if (!dailyDetailPanelEl || dailyDetailPanelEl.classList.contains("hidden")) return;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const pw = dailyDetailPanelEl.offsetWidth || 420;
  const ph = dailyDetailPanelEl.offsetHeight || 300;
  const gap = 16;
  let left = mx + gap;
  let top = my - ph / 2;
  let side = "right";
  if (left + pw + 220 > vw - 10) {
    left = mx - pw - gap;
    side = "left";
  }
  if (top < 10) top = 10;
  if (top + ph > vh - 10) top = vh - ph - 10;
  dailyDetailPanelEl.style.left = left + "px";
  dailyDetailPanelEl.style.top = top + "px";
  _panelSide = side;
  _panelLeft = left;
  _panelWidth = pw;
  _panelTop = top;
}

function renderChart(curves) {
  if (backtestPlaceholderEl) backtestPlaceholderEl.classList.add("hidden");
  if (chartEl) chartEl.classList.remove("hidden");
  if (!chart && window.echarts && chartEl) {
    chart = echarts.init(chartEl);
  }
  if (!chart) {
    statusEl.textContent = "图表组件加载失败，但策略列表和回测功能仍可使用。";
    return;
  }
  const dates = curves[0]?.points.map((item) => item.date) || [];
  const performanceSeries = curves.map((curve) => ({
    name: curve.name,
    type: "line",
    smooth: true,
    showSymbol: false,
    yAxisIndex: 0,
    data: curve.points.map((point) => ({
      value: point.value,
      capital: point.capital,
      position: point.position,
    })),
  }));
  const positionSeries = curves[0]
    ? [
        {
          name: "总仓位比例",
          type: "line",
          smooth: true,
          showSymbol: false,
          yAxisIndex: 1,
          lineStyle: {
            width: 2,
            type: "dashed",
          },
          data: curves[0].points.map((point) => ({
            value: point.position ?? null,
            capital: point.capital,
            position: point.position,
          })),
        },
      ]
    : [];
  const series = [...performanceSeries, ...positionSeries];

  chart.off("click");
  chart.setOption({
    tooltip: {
      trigger: "axis",
      position: (point, params, dom, rect, size) => {
        if (!dailyDetailPanelEl || dailyDetailPanelEl.classList.contains("hidden")) {
          return { top: 60, left: point[0] + 20 };
        }
        const chartRect = chartEl.getBoundingClientRect();
        const tw = size.contentSize[0];
        const th = size.contentSize[1];
        const tGap = 8;
        let tx, ty;
        if (_panelSide === "right") {
          tx = _panelLeft + _panelWidth + tGap - chartRect.left;
        } else {
          tx = _panelLeft - tw - tGap - chartRect.left;
        }
        ty = _panelTop - chartRect.top;
        if (tx < 4) tx = 4;
        if (tx + tw > size.viewSize[0] - 4) tx = size.viewSize[0] - tw - 4;
        if (ty < 4) ty = 4;
        if (ty + th > size.viewSize[1] - 4) ty = size.viewSize[1] - th - 4;
        return [tx, ty];
      },
      formatter: (params) => {
        if (!Array.isArray(params) || !params.length) return "";
        const date = params[0].axisValue || "";
        requestAnimationFrame(() => renderDailyDetail(date));
        const lines = [`${date}`];
        params.forEach((item) => {
          const value = Array.isArray(item.value) ? item.value[1] : item.value?.value ?? item.value;
          const capital = item.data?.capital;
          if (item.seriesName === "总仓位比例") {
            lines.push(`${item.marker}${item.seriesName}：${(Number(value) * 100).toFixed(2)}%`);
          } else {
            lines.push(`${item.marker}${item.seriesName}：${((Number(value) - 1) * 100).toFixed(2)}%`);
          }
          if (capital !== undefined && capital !== null) {
            lines.push(`当日总资产：${formatMoney(capital)}`);
          }
        });
        return lines.join("<br/>");
      },
    },
    legend: {
      top: 8,
      textStyle: { color: "#334155" },
    },
    grid: {
      left: 40,
      right: 24,
      top: 56,
      bottom: 32,
    },
    xAxis: {
      type: "category",
      data: dates,
    },
    yAxis: [
      {
        type: "value",
        axisLabel: {
          formatter: (value) => `${((value - 1) * 100).toFixed(0)}%`,
        },
      },
      {
        type: "value",
        min: 0,
        max: 1,
        axisLabel: {
          formatter: (value) => `${(Number(value) * 100).toFixed(0)}%`,
        },
      },
    ],
    series,
  });
  chart.on("click", (params) => {
    const date = params?.name || params?.axisValue;
    if (date) {
      renderDailyDetail(date);
    }
  });

  if (!chartEl._floatBound) {
    chartEl.addEventListener("mousemove", (e) => {
      _lastMouseX = e.clientX;
      _lastMouseY = e.clientY;
      positionFloatingPanel(_lastMouseX, _lastMouseY);
    });
    chartEl.addEventListener("mouseleave", () => {
      hideDailyDetail();
    });
    chartEl._floatBound = true;
  }
}

function showLastDayDetail() {
  if (!currentDailyPositionDetails.size) return;
  const dates = Array.from(currentDailyPositionDetails.keys()).sort();
  if (dates.length) {
    renderDailyDetail(dates[dates.length - 1]);
  }
}

function clearSortIndicators() {
  tradeTableEl.querySelectorAll("th").forEach((th) => {
    th.classList.remove("sort-asc", "sort-desc");
  });
}

function compareValues(a, b, key, direction) {
  const factor = direction === "asc" ? 1 : -1;
  const left = a?.[key];
  const right = b?.[key];

  if (key.endsWith("_date")) {
    return (new Date(left) - new Date(right)) * factor;
  }
  if (typeof left === "number" && typeof right === "number") {
    return (left - right) * factor;
  }
  return String(left ?? "").localeCompare(String(right ?? ""), "zh-CN") * factor;
}

function sortTradeRecords(key) {
  if (!key) return;
  const direction =
    currentSortState.key === key && currentSortState.direction === "asc" ? "desc" : "asc";
  currentSortState = { key, direction };
  currentTradeRecords = [...currentTradeRecords].sort((a, b) => compareValues(a, b, key, direction));
  renderTradeRecords(currentTradeRecords);
  clearSortIndicators();
  const th = tradeTableEl.querySelector(`th[data-sort-key="${key}"]`);
  if (th) {
    th.classList.add(direction === "asc" ? "sort-asc" : "sort-desc");
  }
}

function renderTradeRecords(records) {
  tradeCountEl.textContent = `共 ${records.length} 笔交易记录`;
  if (!records.length) {
    tradeRecordsBodyEl.innerHTML = `
      <tr>
        <td colspan="14" class="muted empty-cell">当前回测区间内暂无交易记录。</td>
      </tr>
    `;
    return;
  }

  tradeRecordsBodyEl.innerHTML = records
    .map((item) => {
      const pnlClass = item.pnl_amount >= 0 ? "profit" : "loss";
      const sellPrice = Number(item.sell_price || 0);
      const sellAmount = Number(item.sell_amount || 0);
      const holdingDays = item.holding_days ?? 0;
      return `
        <tr>
          <td>${item.trade_no ?? ""}</td>
          <td>${item.trade_type || ""}</td>
          <td>${item.ts_code}<br /><span class="muted">${item.name || ""}</span></td>
          <td>${item.buy_date}</td>
          <td>${item.sell_date}</td>
          <td>${item.sell_reason || "未标注"}</td>
          <td>${Number(item.buy_price).toFixed(2)}</td>
          <td>${sellPrice > 0 ? sellPrice.toFixed(2) : "-"}</td>
          <td>${formatPercent(item.position_weight)}</td>
          <td>${formatMoney(item.buy_amount)}</td>
          <td>${sellAmount > 0 ? formatMoney(item.sell_amount) : "-"}</td>
          <td class="${pnlClass}">${formatMoney(item.pnl_amount)}</td>
          <td class="${pnlClass}">${formatPercent(item.return_rate)}</td>
          <td>${holdingDays > 0 ? holdingDays : "-"}</td>
        </tr>
      `;
    })
    .join("");
}

async function pollBacktestJob(jobId) {
  if (currentPollingTimer) {
    clearTimeout(currentPollingTimer);
    currentPollingTimer = null;
  }

  const response = await fetch(`/api/backtest/jobs/${jobId}`);
  const job = await response.json();
  if (!response.ok) {
    throw new Error(job.detail || "查询回测进度失败");
  }

  setProgress(job.progress, job.message);
  statusEl.textContent = job.message || "回测执行中，请稍候...";

  if (job.status === "completed") {
    if (backtestPlaceholderEl) backtestPlaceholderEl.classList.add("hidden");
    if (chartEl) chartEl.classList.remove("hidden");

    updateJobMeta(jobId, job.result);
    currentDailyPositionDetails = buildDailyPositionMap(job.result.daily_position_details);
    renderMetrics(job.result.metrics);
    renderSignalSummary(job.result.signal_summary);
    renderTradeSummary(job.result.trade_summary);
    currentTradeRecords = job.result.trade_records || [];
    renderTradeRecords(currentTradeRecords);
    renderChart(job.result.curves);
    statusEl.textContent = `回测完成：${job.result.asset}，策略 ${job.result.strategy.name}`;
    submitButton.disabled = false;
    hideProgress();
    return;
  }

  if (job.status === "failed") {
    updateJobMeta(jobId, null);
    submitButton.disabled = false;
    hideProgress();
    statusEl.textContent = `回测失败：${job.error || job.message || "未知错误"}`;
    return;
  }

  currentPollingTimer = setTimeout(() => {
    pollBacktestJob(jobId).catch((error) => {
      submitButton.disabled = false;
      statusEl.textContent = `回测失败：${error.message}`;
    });
  }, 1000);
}

formEl.addEventListener("submit", async (event) => {
  event.preventDefault();
  submitButton.disabled = true;
  clearPreviousResult();
  setProgress(0, "正在创建回测任务...");
  showLoadingTips();
  statusEl.textContent = "回测执行中，请稍候...";

  const formData = new FormData(formEl);
  const payload = {
    strategy_name: formData.get("strategy_name"),
    start_date: formData.get("start_date"),
    end_date: formData.get("end_date"),
    initial_capital: Number(formData.get("initial_capital")),
    commission_rate: Number(formData.get("commission_rate")),
    stamp_duty_rate: Number(formData.get("stamp_duty_rate")),
    strategy_params: collectStrategyParams(),
  };

  try {
    if (!Number.isFinite(payload.initial_capital) || payload.initial_capital <= 0) {
      throw new Error("请输入有效的初始资金。");
    }

    const response = await fetch("/api/backtest/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) {
      throw new Error(result.detail || "回测失败");
    }
    updateJobMeta(result.job_id, null);
    statusEl.textContent = `回测执行中，请稍候... job_id=${result.job_id}`;
    await pollBacktestJob(result.job_id);
  } catch (error) {
    statusEl.textContent = `回测失败：${error.message}`;
    submitButton.disabled = false;
  } finally {
    if (!submitButton.disabled) {
      hideProgress();
    }
  }
});

strategySelectEl.addEventListener("change", (event) => {
  const strategy = strategyMap.get(event.target.value);
  renderStrategyParams(strategy);
});

tradeTableEl.querySelectorAll("th[data-sort-key]").forEach((th) => {
  th.addEventListener("click", () => sortTradeRecords(th.dataset.sortKey));
});

window.addEventListener("resize", () => {
  if (chart) {
    chart.resize();
  }
});

loadStrategies();
