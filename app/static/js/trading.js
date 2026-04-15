/* ═══════════════════════════════════════════
   实盘交易模块前端逻辑
   ═══════════════════════════════════════════ */
(function () {
  "use strict";

  // ── Tab 切换 ──────────────────────────────

  const tabs = document.querySelectorAll(".tab[data-tab]");
  const tabContents = document.querySelectorAll("[data-tab-content]");

  tabs.forEach((btn) => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      tabs.forEach((t) => t.classList.toggle("active", t === btn));
      tabContents.forEach((el) => {
        if (el.dataset.tabContent === target) {
          el.classList.remove("hidden");
        } else {
          el.classList.add("hidden");
        }
      });
    });
  });

  // ── DOM 引用 ──────────────────────────────

  const statusEl = document.getElementById("qmt-status");
  const connectBtn = document.getElementById("qmt-connect-btn");
  const disconnectBtn = document.getElementById("qmt-disconnect-btn");
  const runBtn = document.getElementById("trading-run-btn");
  const strategySelect = document.getElementById("trading-strategy-select");
  const priceTypeSelect = document.getElementById("trading-price-type");
  const lookbackInput = document.getElementById("trading-lookback");
  const liveStartInput = document.getElementById("trading-live-start");
  const accountPanel = document.getElementById("trading-account-panel");
  const accountInfoEl = document.getElementById("trading-account-info");
  const positionsBody = document.getElementById("trading-positions-body");
  const ordersBody = document.getElementById("trading-orders-body");
  const orderCountEl = document.getElementById("trading-order-count");
  const logEl = document.getElementById("trading-log");
  const progressContainer = document.getElementById("trading-progress");
  const progressMsg = document.getElementById("trading-progress-msg");
  const progressPct = document.getElementById("trading-progress-pct");
  const progressBar = document.getElementById("trading-progress-bar");
  const queryOrdersBtn = document.getElementById("qmt-query-orders-btn");
  const qmtOrdersSection = document.getElementById("qmt-orders-section");
  const qmtOrdersBody = document.getElementById("qmt-orders-body");
  const qmtOrderCountEl = document.getElementById("qmt-order-count");
  const refreshLogBtn = document.getElementById("qmt-refresh-log-btn");
  const pendingBuysSection = document.getElementById("pending-buys-section");
  const pendingBuysBody = document.getElementById("pending-buys-body");
  const pendingBuysCount = document.getElementById("pending-buys-count");

  const schedulerStartBtn = document.getElementById("scheduler-start-btn");
  const schedulerStopBtn = document.getElementById("scheduler-stop-btn");
  const schedulerStatusBar = document.getElementById("scheduler-status-bar");
  const schedulerStatusText = document.getElementById("scheduler-status-text");
  const toggleBuyExisting = document.getElementById("toggle-buy-existing");
  const toggleAllowSell = document.getElementById("toggle-allow-sell");

  let isConnected = false;
  let isSchedulerRunning = false;
  let pollingTimer = null;
  let postCompleteTimer = null;
  let schedulerPollTimer = null;
  let shouldAutoScrollLog = true;
  let logOffset = 0;
  const MAX_LOG_DOM_NODES = 800;
  const TRIM_LOG_DOM_TO = 500;

  // ── 工具 ──────────────────────────────────

  function getSelectedFundRatio() {
    const checked = document.querySelector('input[name="fund-ratio"]:checked');
    return checked ? parseFloat(checked.value) : 1.0;
  }

  // ── 加载策略列表 ──────────────────────────

  async function loadStrategies() {
    try {
      const res = await fetch("/api/strategies");
      if (!res.ok) return;
      const strategies = await res.json();
      strategySelect.innerHTML = "";
      strategies.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s.path.split(/[/\\]/).pop().replace(".py", "");
        opt.textContent = s.name;
        strategySelect.appendChild(opt);
      });
    } catch (_) {
      /* ignore */
    }
  }

  loadStrategies();

  if (liveStartInput && !liveStartInput.value) {
    liveStartInput.value = new Date().toISOString().slice(0, 10);
  }

  // ── 连接 / 断开 ──────────────────────────

  connectBtn.addEventListener("click", async () => {
    connectBtn.disabled = true;
    connectBtn.textContent = "连接中...";
    try {
      const res = await fetch("/api/trading/connect", { method: "POST" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "连接失败");
      setConnected(true);
      await refreshAccount();
      checkSchedulerStatus();
    } catch (err) {
      alert("连接 QMT 失败：" + err.message);
      setConnected(false);
    } finally {
      connectBtn.textContent = "连接 QMT";
      connectBtn.disabled = isConnected;
    }
  });

  disconnectBtn.addEventListener("click", async () => {
    try {
      await fetch("/api/trading/disconnect", { method: "POST" });
    } catch (_) {
      /* ignore */
    }
    setConnected(false);
    setSchedulerRunning(false);
    accountPanel.classList.add("hidden");
  });

  function setConnected(connected) {
    isConnected = connected;
    statusEl.textContent = connected ? "已连接" : "未连接";
    statusEl.className = "qmt-status " + (connected ? "connected" : "disconnected");
    connectBtn.disabled = connected;
    disconnectBtn.disabled = !connected;
    runBtn.disabled = !connected;
    schedulerStartBtn.disabled = !connected || isSchedulerRunning;
    schedulerStopBtn.disabled = !connected || !isSchedulerRunning;
    queryOrdersBtn.disabled = !connected;
    refreshLogBtn.disabled = !connected;
  }

  // ── 调度器 ────────────────────────────────

  schedulerStartBtn.addEventListener("click", async () => {
    if (!isConnected) return;
    schedulerStartBtn.disabled = true;
    schedulerStartBtn.textContent = "启动中...";
    try {
      const body = {
        strategy_name: strategySelect.value,
        fund_ratio: getSelectedFundRatio(),
        buy_existing: toggleBuyExisting.checked,
        allow_sell: toggleAllowSell.checked,
        price_type: priceTypeSelect.value,
        lookback_days: parseInt(lookbackInput.value, 10) || 250,
        live_start_date: liveStartInput.value || "",
      };
      const res = await fetch("/api/trading/scheduler/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "启动失败");
      setSchedulerRunning(true);
      startSchedulerPolling();
    } catch (err) {
      alert("开启策略失败：" + err.message);
    } finally {
      schedulerStartBtn.textContent = "开启策略";
      schedulerStartBtn.disabled = isSchedulerRunning;
    }
  });

  schedulerStopBtn.addEventListener("click", async () => {
    try {
      await fetch("/api/trading/scheduler/stop", { method: "POST" });
    } catch (_) {
      /* ignore */
    }
    setSchedulerRunning(false);
    stopSchedulerPolling();
  });

  function setSchedulerRunning(running) {
    isSchedulerRunning = running;
    schedulerStartBtn.disabled = !isConnected || running;
    schedulerStopBtn.disabled = !isConnected || !running;
    strategySelect.disabled = running;
    priceTypeSelect.disabled = running;
    lookbackInput.disabled = running;
    liveStartInput.disabled = running;
    document.querySelectorAll('input[name="fund-ratio"]').forEach((r) => (r.disabled = running));
    toggleBuyExisting.disabled = running;
    toggleAllowSell.disabled = running;
  }

  function startSchedulerPolling() {
    stopSchedulerPolling();
    schedulerPollTimer = setInterval(pollSchedulerStatus, 3000);
    pollSchedulerStatus();
  }

  function stopSchedulerPolling() {
    if (schedulerPollTimer) {
      clearInterval(schedulerPollTimer);
      schedulerPollTimer = null;
    }
  }

  async function pollSchedulerStatus() {
    try {
      const res = await fetch(`/api/trading/scheduler/status?log_offset=${logOffset}`);
      if (!res.ok) return;
      const s = await res.json();
      applySchedulerStatus(s);

      if (!s.running) {
        stopSchedulerPolling();
      }
    } catch (_) {
      /* ignore */
    }
  }

  async function checkSchedulerStatus() {
    try {
      const res = await fetch(`/api/trading/scheduler/status?log_offset=${logOffset}`);
      if (!res.ok) return;
      const s = await res.json();
      applySchedulerStatus(s);
      if (s.running && !schedulerPollTimer) {
        startSchedulerPolling();
      }
    } catch (_) {
      /* ignore */
    }
  }

  function applySchedulerStatus(s) {
    setSchedulerRunning(Boolean(s.running));
    updateSchedulerStatusBar(s);

    if (s.execution_log && s.execution_log.length > 0) {
      appendLog(s.execution_log);
    }
    if (typeof s.log_total === "number") {
      logOffset = s.log_total;
    }
    if (s.today_orders && s.today_orders.length > 0) {
      renderOrders(s.today_orders);
    }
    renderPendingSells(s.pending_sell_signals || []);
    renderPendingBuys(s.pending_buy_signals || []);
    if (s.account_info) renderAccountInfo(s.account_info);
    if (s.positions) renderPositions(s.positions);
  }

  function updateSchedulerStatusBar(s) {
    schedulerStatusBar.classList.remove("hidden");
    if (s.running) {
      schedulerStatusBar.className = "scheduler-status-bar active";
      let text = `策略运行中: ${s.strategy_name || ""}`;
      text += `  |  资金: ${Math.round(s.fund_ratio * 100)}%`;
      text += `  |  买入已持仓: ${s.buy_existing ? "是" : "否"}`;
      text += `  |  自动卖出: ${s.allow_sell ? "是" : "否"}`;
      if (s.pending_buy_signals && s.pending_buy_signals.length > 0) {
        text += `  |  待买入: ${s.pending_buy_signals.length}只`;
      }
      if (s.next_execution) text += `\n下次执行: ${s.next_execution}`;
      if (s.last_execution) text += `  |  上次执行: ${s.last_execution}`;
      if (s.today_executed) text += "  (今日卖出已执行)";
      schedulerStatusText.textContent = text;
    } else if (s.last_execution) {
      schedulerStatusBar.className = "scheduler-status-bar stopped";
      schedulerStatusText.textContent = `策略已停止  |  上次执行: ${s.last_execution}`;
    } else {
      schedulerStatusBar.className = "scheduler-status-bar";
      schedulerStatusText.textContent = "策略未启动";
    }
  }

  // ── 账户刷新 ──────────────────────────────

  async function refreshAccount() {
    try {
      const res = await fetch("/api/trading/account");
      const data = await res.json();
      if (!data.connected) {
        setConnected(false);
        accountPanel.classList.add("hidden");
        return;
      }
      setConnected(true);
      renderAccountInfo(data.account_info);
      renderPositions(data.positions);
      accountPanel.classList.remove("hidden");
    } catch (_) {
      /* ignore */
    }
  }

  function renderAccountInfo(info) {
    if (!info) {
      accountInfoEl.innerHTML = '<span class="muted">暂无数据</span>';
      return;
    }
    accountInfoEl.innerHTML = [
      stat("总资产", fmt(info.total_asset)),
      stat("可用资金", fmt(info.available_cash)),
      stat("持仓市值", fmt(info.market_value)),
      stat("冻结资金", fmt(info.frozen_cash)),
    ].join("");
  }

  function renderPositions(positions) {
    if (!positions || positions.length === 0) {
      positionsBody.innerHTML = '<tr><td colspan="6" class="muted empty-cell">暂无持仓</td></tr>';
      return;
    }
    positionsBody.innerHTML = positions
      .map((p) => {
        const availableVolume = Number(p.available_volume || 0);
        const volume = Number(p.volume || 0);
        const costPrice = Number(p.cost_price || 0);
        const marketValue = Number(p.market_value || 0);
        const profit = Number(p.profit || 0);
        const profitRate = Number(p.profit_rate || 0);
        const profitClass = profit > 0 ? "profit" : profit < 0 ? "loss" : "";
        const ratioClass = profitRate > 0 ? "profit" : profitRate < 0 ? "loss" : "";
        return `<tr>
        <td>${escHtml(p.ts_code)}</td>
        <td>${availableVolume} / ${volume}</td>
        <td>${costPrice.toFixed(2)}</td>
        <td>${fmt(marketValue)}</td>
        <td class="${profitClass}">${fmt(profit)}</td>
        <td class="${ratioClass}">${fmtPct(profitRate)}</td>
      </tr>`;
      })
      .join("");
  }

  // ── 查询 QMT 委托 ──────────────────────────

  queryOrdersBtn.addEventListener("click", async () => {
    if (!isConnected) return;
    queryOrdersBtn.disabled = true;
    queryOrdersBtn.textContent = "查询中...";
    try {
      const res = await fetch("/api/trading/orders");
      if (!res.ok) {
        const d = await res.json();
        throw new Error(d.detail || "查询失败");
      }
      const data = await res.json();
      renderQmtOrders(data.orders || []);
    } catch (err) {
      alert("查询委托失败：" + err.message);
    } finally {
      queryOrdersBtn.textContent = "查询QMT委托";
      queryOrdersBtn.disabled = !isConnected;
    }
  });

  function renderQmtOrders(orders) {
    qmtOrdersSection.classList.remove("hidden");
    qmtOrderCountEl.textContent = `共 ${orders.length} 笔`;

    if (!orders.length) {
      qmtOrdersBody.innerHTML = '<tr><td colspan="8" class="muted empty-cell">暂无委托</td></tr>';
      return;
    }
    qmtOrdersBody.innerHTML = orders
      .map((o) => {
        const statusCls = getQmtStatusClass(o.order_status_code || 0, o.order_status);
        return `<tr>
          <td>${o.stock_code}</td>
          <td class="${o.order_type === '买入' ? 'direction-buy' : 'direction-sell'}">${o.order_type}</td>
          <td>${o.order_volume}</td>
          <td>${Number(o.price).toFixed(2)}</td>
          <td>${o.traded_volume || 0}</td>
          <td>${o.traded_volume ? Number(o.traded_price).toFixed(2) : "-"}</td>
          <td class="${statusCls}">${o.order_status}</td>
          <td title="${escHtml(o.status_msg || '')}">${truncate(o.status_msg || "", 20)}</td>
        </tr>`;
      })
      .join("");
  }

  function getQmtStatusClass(code, label) {
    if (code === 56) return "order-status-success";
    if (code === 55) return "order-status-partial";
    if (code === 57 || code === 54 || code === 53) return "order-status-failed";
    if (label && (label.includes("已成") || label.includes("部成"))) return "order-status-success";
    if (label && (label.includes("废单") || label.includes("撤"))) return "order-status-failed";
    return "";
  }

  // ── 刷新 QMT 日志 ──────────────────────────

  refreshLogBtn.addEventListener("click", async () => {
    if (!isConnected) return;
    refreshLogBtn.disabled = true;
    try {
      const res = await fetch("/api/trading/logs");
      const data = await res.json();
      if (data.logs && data.logs.length) {
        renderLog(data.logs);
      }
    } catch (_) {
      /* ignore */
    } finally {
      refreshLogBtn.disabled = !isConnected;
    }
  });

  // ── 手动执行交易 ──────────────────────────

  runBtn.addEventListener("click", async () => {
    if (!isConnected) return;
    runBtn.disabled = true;
    runBtn.textContent = "执行中...";
    resetResults();
    progressContainer.classList.remove("hidden");

    try {
      const body = {
        strategy_name: strategySelect.value,
        price_type: priceTypeSelect.value,
        lookback_days: parseInt(lookbackInput.value, 10) || 250,
      };
      const res = await fetch("/api/trading/run", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "启动失败");
      startPolling(data.job_id);
    } catch (err) {
      alert("执行失败：" + err.message);
      runBtn.disabled = false;
      runBtn.textContent = "手动执行一次";
      progressContainer.classList.add("hidden");
    }
  });

  // ── 轮询任务状态 ──────────────────────────

  function startPolling(jobId) {
    stopAllTimers();
    pollingTimer = setInterval(() => pollJob(jobId), 1500);
    pollJob(jobId);
  }

  async function pollJob(jobId) {
    try {
      const res = await fetch(`/api/trading/jobs/${jobId}`);
      if (!res.ok) return;
      const job = await res.json();

      updateProgress(job.progress, job.message);
      if (job.orders && job.orders.length > 0) renderOrders(job.orders);
      if (job.execution_log && job.execution_log.length > 0) renderLog(job.execution_log);
      if (job.account_info) renderAccountInfo(job.account_info);
      if (job.current_positions && job.current_positions.length > 0)
        renderPositions(job.current_positions);

      if (job.status === "completed" || job.status === "failed") {
        clearInterval(pollingTimer);
        pollingTimer = null;
        runBtn.disabled = false;
        runBtn.textContent = "手动执行一次";
        if (job.status === "failed" && job.error) {
          appendLog(["错误: " + job.error]);
        }
        await refreshAccount();
        startPostCompletePolling(jobId);
      }
    } catch (_) {
      /* ignore */
    }
  }

  function startPostCompletePolling(jobId) {
    let rounds = 0;
    const maxRounds = 10;
    postCompleteTimer = setInterval(async () => {
      rounds++;
      if (rounds > maxRounds) {
        clearInterval(postCompleteTimer);
        postCompleteTimer = null;
        return;
      }
      try {
        const res = await fetch(`/api/trading/jobs/${jobId}`);
        if (!res.ok) return;
        const job = await res.json();
        if (job.execution_log && job.execution_log.length > 0) {
          renderLog(job.execution_log);
        }
      } catch (_) {
        /* ignore */
      }
    }, 2000);
  }

  function stopAllTimers() {
    if (pollingTimer) {
      clearInterval(pollingTimer);
      pollingTimer = null;
    }
    if (postCompleteTimer) {
      clearInterval(postCompleteTimer);
      postCompleteTimer = null;
    }
  }

  // ── 渲染辅助 ──────────────────────────────

  function updateProgress(progress, message) {
    progressPct.textContent = Math.round(progress) + "%";
    progressMsg.textContent = message || "";
    progressBar.style.width = progress + "%";
  }

  function renderPendingSells(items) {
    const section = document.getElementById("pending-sells-section");
    const body = document.getElementById("pending-sells-body");
    const count = document.getElementById("pending-sells-count");
    if (!section) return;
    if (!items || items.length === 0) {
      section.classList.add("hidden");
      return;
    }
    section.classList.remove("hidden");
    count.textContent = `共 ${items.length} 只`;
    body.innerHTML = items
      .map(
        (p) =>
          `<tr><td>${escHtml(p.ts_code)}</td><td>${escHtml(p.name || "")}</td><td>${escHtml(p.reason || "")}</td></tr>`
      )
      .join("");
  }

  function renderPendingBuys(items) {
    if (!items || items.length === 0) {
      pendingBuysSection.classList.add("hidden");
      return;
    }
    pendingBuysSection.classList.remove("hidden");
    pendingBuysCount.textContent = `共 ${items.length} 只`;
    pendingBuysBody.innerHTML = items
      .map(
        (p) =>
          `<tr><td>${escHtml(p.ts_code)}</td><td>${escHtml(p.name || "")}</td></tr>`
      )
      .join("");
  }

  function renderOrders(orders) {
    const sells = orders.filter((o) => o.direction === "sell").length;
    const buys = orders.filter((o) => o.direction === "buy").length;
    orderCountEl.textContent = `共 ${orders.length} 笔 (卖${sells} 买${buys})`;

    ordersBody.innerHTML = orders
      .map((o) => {
        const dirClass = o.direction === "buy" ? "direction-buy" : "direction-sell";
        const dirLabel = o.direction === "buy" ? "买入" : "卖出";
        const statusClass =
          o.status === "submitted"
            ? "order-status-submitted"
            : o.status === "failed"
            ? "order-status-failed"
            : "";
        const statusLabel =
          o.status === "submitted"
            ? "已提交"
            : o.status === "failed"
            ? "失败"
            : o.status === "pending"
            ? "待执行"
            : o.status;
        return `<tr>
          <td>${o.ts_code}</td>
          <td>${o.name || ""}</td>
          <td class="${dirClass}">${dirLabel}</td>
          <td>${o.volume}</td>
          <td>${o.price.toFixed(2)}</td>
          <td>${fmt(o.amount)}</td>
          <td>${o.price_type}</td>
          <td class="${statusClass}">${statusLabel}</td>
        </tr>`;
      })
      .join("");
  }

  function isLogNearBottom() {
    return logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 40;
  }

  function syncLogAutoScrollState() {
    shouldAutoScrollLog = isLogNearBottom();
  }

  logEl.addEventListener("scroll", syncLogAutoScrollState);

  function appendLog(newLines) {
    const fragment = document.createDocumentFragment();
    for (const l of newLines) {
      const p = document.createElement("p");
      p.className = getLogClass(l);
      p.textContent = l;
      fragment.appendChild(p);
    }
    logEl.appendChild(fragment);
    // DOM 节点过多时截断最旧的
    while (logEl.children.length > MAX_LOG_DOM_NODES) {
      logEl.removeChild(logEl.firstChild);
    }
    if (shouldAutoScrollLog) {
      logEl.scrollTop = logEl.scrollHeight;
    }
  }

  function renderLog(logs) {
    logEl.innerHTML = "";
    appendLog(logs);
  }

  function getLogClass(line) {
    if (line.includes("[成交]")) return "log-success";
    if (line.includes("[失败]") || line.includes("失败") || line.includes("报错"))
      return "log-error";
    if (line.includes("[委托]")) return "log-order";
    if (line.includes("[下单]")) return "log-submit";
    if (line.includes("[异步回报]") && line.includes("已受理")) return "log-info";
    if (line.includes("[异步回报]") && line.includes("失败")) return "log-error";
    if (line.includes("[连接]") || line.includes("[账户]")) return "log-warn";
    if (line.includes("废单")) return "log-error";
    if (line.includes("[错误]")) return "log-error";
    if (line.includes("已成")) return "log-success";
    if (line.includes("====")) return "log-separator";
    return "log-default";
  }

  function resetResults() {
    ordersBody.innerHTML =
      '<tr><td colspan="8" class="muted empty-cell">执行后显示订单</td></tr>';
    orderCountEl.textContent = "";
    logEl.innerHTML = '<p class="muted">正在启动...</p>';
    shouldAutoScrollLog = true;
    updateProgress(0, "准备中...");
    qmtOrdersSection.classList.add("hidden");
  }

  // ── 工具函数 ──────────────────────────────

  function fmt(n) {
    if (n == null) return "-";
    return Number(n).toLocaleString("zh-CN", {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
  }

  function fmtPct(n) {
    if (n == null) return "-";
    return `${(Number(n) * 100).toFixed(2)}%`;
  }

  function stat(label, value) {
    return `<div class="stat"><span class="stat-label">${label}</span><span class="stat-value">${value}</span></div>`;
  }

  function escHtml(s) {
    const el = document.createElement("span");
    el.textContent = s;
    return el.innerHTML;
  }

  // ── 日志管理按钮 ──────────────────────────────

  const saveLogBtn = document.getElementById("save-log-btn");
  const clearLogBtn = document.getElementById("clear-log-btn");

  if (saveLogBtn) {
    saveLogBtn.addEventListener("click", () => {
      const lines = Array.from(logEl.children).map((p) => p.textContent);
      if (lines.length === 0) return;
      const blob = new Blob([lines.join("\n")], { type: "text/plain;charset=utf-8" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      const today = new Date().toISOString().slice(0, 10);
      a.download = `scheduler_log_${today}.txt`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  if (clearLogBtn) {
    clearLogBtn.addEventListener("click", () => {
      logEl.innerHTML = "";
    });
  }

  function truncate(s, maxLen) {
    if (!s || s.length <= maxLen) return s;
    return s.substring(0, maxLen) + "…";
  }

  async function initializeTradingState() {
    await refreshAccount();
    await checkSchedulerStatus();
  }

  initializeTradingState();
})();
