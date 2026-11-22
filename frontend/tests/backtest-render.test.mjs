import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/",
  pretendToBeVisual: true,
});
for (const name of [
  "window",
  "document",
  "navigator",
  "HTMLElement",
  "Element",
  "Node",
  "Event",
  "MouseEvent",
  "KeyboardEvent",
  "getComputedStyle",
  "SVGElement",
  "ShadowRoot",
])
  Object.defineProperty(globalThis, name, {
    value: name === "window" ? dom.window : dom.window[name],
    configurable: true,
    writable: true,
  });
const computed = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computed(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { ConfigProvider, App } = await import("antd");
const { panel } = await load({ panel: "./src/components/backtest" });

let requestedDates = [];
let tradesPayload = { items: [], total: 0 };
globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  const position = path.match(/^\/backtests\/run\/positions(?:\?|$)/);
  if (position) {
    const date = new URL(String(url), "http://127.0.0.1").searchParams.get(
      "date",
    );
    requestedDates.push(date);
    return new Response(JSON.stringify({ items: [], total: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  }
  if (path.match(/^\/backtests\/run-trades\/trades(?:\?|$)/)) {
    return new Response(JSON.stringify(tradesPayload), {
      headers: { "Content-Type": "application/json" },
    });
  }
  throw Error(`Unexpected request ${path}`);
};

async function waitFor(check) {
  const start = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - start > 5000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 30)));
    }
  }
}

async function mount(component) {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(App, null, component),
      ),
    ),
  );
  return () => act(async () => root.unmount());
}

test("legacy start_date equity point can still query positions", async () => {
  const close = await mount(
    React.createElement(panel.PositionsPanel, {
      runId: "run",
      dates: ["2025-03-03", "2025-03-04"],
      date: null,
      onDate: () => {},
    }),
  );
  try {
    await waitFor(() => assert.deepEqual(requestedDates, ["2025-03-04"]));
  } finally {
    await close();
  }
});

function baseDetail(overrides = {}) {
  return {
    id: "bt-cost-1",
    name: "费用展示测试",
    status: "finished",
    strategy: "topk",
    start_date: "2025-01-02",
    end_date: "2025-03-31",
    created_at: "2026-11-21T08:00:00Z",
    is_synthetic: false,
    metrics: null,
    execution_costs: {
      cost_mode: "itemized",
      commission_rate: 0.0003,
      tax_mode: "historical",
      stamp_tax_rate: 0.0005,
      transfer_fee_rate: 0.00001,
      impact_cost: 0.0005,
      min_cost: 5,
    },
    rebalance_policy: {
      frequency: "weekly",
      scheduled_count: 10,
      dates: [],
      timing: "开盘",
      exit_policy: "按计划调仓",
    },
    ...overrides,
  };
}

function readCostTableRows() {
  const rows = [];
  for (const tr of document.querySelectorAll(
    '[aria-label="费用构成表"] tbody tr',
  )) {
    const cells = [...tr.querySelectorAll("td")].map((td) =>
      td.textContent.trim(),
    );
    if (cells.length === 2) rows.push([cells[0], cells[1]]);
  }
  return rows;
}

test("CostsPanel hides inapplicable legacy_fee in itemized mode and matches unrounded total", async () => {
  const cost_summary = {
    commission: 28600.60140006108,
    stamp_tax: 19652.590335896595,
    transfer_fee: 695.1060577363468,
    impact: 5260.125746619963,
    legacy_fee: 0,
    total: 54208.42354031398,
  };
  const detail = baseDetail({ cost_summary });
  const visible = panel.costSummaryRows(cost_summary, detail.execution_costs);
  const componentSum = visible
    .filter((row) => row.key !== "total")
    .reduce((acc, row) => acc + (row.value ?? 0), 0);
  assert.ok(Math.abs(componentSum - cost_summary.total) < 1e-9);
  assert.equal(cost_summary.legacy_fee, 0);

  const close = await mount(React.createElement(panel.CostsPanel, { detail }));
  try {
    const rows = readCostTableRows();
    assert.deepEqual(rows, [
      ["券商佣金", "28,600.60"],
      ["印花税", "19,652.59"],
      ["过户费", "695.11"],
      ["冲击成本", "5,260.13"],
      ["合计", "54,208.42"],
    ]);
    assert.doesNotMatch(document.body.textContent, /综合费用|旧版费用/);
    assert.match(
      document.body.textContent,
      /冲击成本在两种计费模式下都单独计入合计/,
    );
  } finally {
    await close();
  }
});

test("CostsPanel preserves true zero values as 0.00 in itemized zero-trade run", async () => {
  const cost_summary = {
    commission: 0,
    stamp_tax: 0,
    transfer_fee: 0,
    impact: 0,
    legacy_fee: 0,
    total: 0,
  };
  const close = await mount(
    React.createElement(panel.CostsPanel, {
      detail: baseDetail({ cost_summary }),
    }),
  );
  try {
    assert.deepEqual(readCostTableRows(), [
      ["券商佣金", "0.00"],
      ["印花税", "0.00"],
      ["过户费", "0.00"],
      ["冲击成本", "0.00"],
      ["合计", "0.00"],
    ]);
  } finally {
    await close();
  }
});

test("CostsPanel renders legacy_fee + impact with 旧版费用（综合费率） and keeps true zero as 0.00", async () => {
  const cost_summary = {
    commission: 0,
    stamp_tax: 0,
    transfer_fee: 0,
    impact: 200.125,
    legacy_fee: 500.375,
    total: 700.5,
  };
  const detail = baseDetail({
    execution_costs: {
      ...baseDetail().execution_costs,
      cost_mode: "legacy",
    },
    cost_summary,
  });
  const visible = panel.costSummaryRows(cost_summary, detail.execution_costs);
  const componentSum = visible
    .filter((row) => row.key !== "total")
    .reduce((acc, row) => acc + (row.value ?? 0), 0);
  assert.equal(componentSum, cost_summary.total);

  const close = await mount(React.createElement(panel.CostsPanel, { detail }));
  try {
    assert.deepEqual(readCostTableRows(), [
      ["冲击成本", "200.13"],
      ["旧版费用（综合费率）", "500.38"],
      ["合计", "700.50"],
    ]);
    assert.match(
      document.body.textContent,
      /冲击成本在两种计费模式下都单独计入合计；旧版费用（综合费率）按旧版买卖综合费率计收，已计入合计/,
    );
  } finally {
    await close();
  }

  const zeroLegacy = await mount(
    React.createElement(panel.CostsPanel, {
      detail: baseDetail({
        execution_costs: {
          ...baseDetail().execution_costs,
          cost_mode: "legacy",
        },
        cost_summary: {
          commission: 0,
          stamp_tax: 0,
          transfer_fee: 0,
          impact: 0,
          legacy_fee: 0,
          total: 0,
        },
      }),
    }),
  );
  try {
    assert.deepEqual(readCostTableRows(), [
      ["冲击成本", "0.00"],
      ["旧版费用（综合费率）", "0.00"],
      ["合计", "0.00"],
    ]);
  } finally {
    await zeroLegacy();
  }
});

test("CostsPanel keeps legacy row and renders missing fields as — when execution_costs is absent without claiming legacy fee charged", async () => {
  const close = await mount(
    React.createElement(panel.CostsPanel, {
      detail: baseDetail({
        execution_costs: undefined,
        rebalance_policy: undefined,
        cost_summary: {
          impact: 0,
          total: 120,
        },
      }),
    }),
  );
  try {
    assert.deepEqual(readCostTableRows(), [
      ["券商佣金", "—"],
      ["印花税", "—"],
      ["过户费", "—"],
      ["冲击成本", "0.00"],
      ["旧版费用（综合费率）", "—"],
      ["合计", "120.00"],
    ]);
    assert.match(
      document.body.textContent,
      /当前记录未明确计费模式，缺失项以「—」区分/,
    );
    assert.doesNotMatch(
      document.body.textContent,
      /旧版费用（综合费率）按旧版买卖综合费率计收，已计入合计/,
    );
  } finally {
    await close();
  }
});

test("CostsPanel under explicit legacy mode with missing legacy_fee displays — without claiming included in total", async () => {
  const close = await mount(
    React.createElement(panel.CostsPanel, {
      detail: baseDetail({
        execution_costs: {
          ...baseDetail().execution_costs,
          cost_mode: "legacy",
        },
        cost_summary: {
          commission: 0,
          stamp_tax: 0,
          transfer_fee: 0,
          impact: 15,
          total: 15,
        },
      }),
    }),
  );
  try {
    assert.deepEqual(readCostTableRows(), [
      ["冲击成本", "15.00"],
      ["旧版费用（综合费率）", "—"],
      ["合计", "15.00"],
    ]);
    assert.match(
      document.body.textContent,
      /当前旧版模式记录未保存旧版费用金额，缺失项以「—」区分/,
    );
    assert.doesNotMatch(
      document.body.textContent,
      /旧版费用（综合费率）按旧版买卖综合费率计收，已计入合计/,
    );
    assert.doesNotMatch(document.body.textContent, /未明确计费模式/);
  } finally {
    await close();
  }
});

test("TradesPanel shares applicability rule: hides itemized legacy_fee=0 while keeping true stamp_tax=0, shows legacy label, and preserves unknown mode", async () => {
  // 1. Itemized buy fill: stamp_tax=0 is applicable true zero, legacy_fee=0 is structural and hidden
  tradesPayload = {
    total: 1,
    items: [
      {
        date: "2025-03-04",
        instrument: "600000.SH",
        name: "浦发银行",
        side: "buy",
        amount: 1000,
        price: 10.5,
        value: 10500,
        cost: 8.25,
        cost_breakdown: {
          commission: 5.0,
          stamp_tax: 0,
          transfer_fee: 0.1,
          impact: 3.15,
          legacy_fee: 0,
          total: 8.25,
        },
      },
    ],
  };
  const closeItemized = await mount(
    React.createElement(panel.TradesPanel, {
      runId: "run-trades",
      costMode: "itemized",
    }),
  );
  try {
    const expandBtn = await waitFor(() => {
      const btn = document.querySelector(".ant-table-row-expand-icon");
      assert.ok(btn);
      return btn;
    });
    await act(async () => {
      expandBtn.click();
    });
    await waitFor(() => assert.match(document.body.textContent, /券商佣金/));
    assert.match(document.body.textContent, /印花税0\.00|印花税\s*0\.00/);
    assert.match(document.body.textContent, /冲击成本/);
    assert.doesNotMatch(
      document.body.textContent,
      /旧版费用（综合费率）|综合费用/,
    );
  } finally {
    await closeItemized();
  }

  // 2. Legacy fill: uses 旧版费用（综合费率） + 冲击成本, hides inapplicable itemized zero fields
  tradesPayload = {
    total: 1,
    items: [
      {
        date: "2025-03-04",
        instrument: "600000.SH",
        name: "浦发银行",
        side: "buy",
        amount: 1000,
        price: 10.5,
        value: 10500,
        cost: 12.6,
        cost_breakdown: {
          commission: 0,
          stamp_tax: 0,
          transfer_fee: 0,
          impact: 2.1,
          legacy_fee: 10.5,
          total: 12.6,
        },
      },
    ],
  };
  const closeLegacy = await mount(
    React.createElement(panel.TradesPanel, {
      runId: "run-trades",
      costMode: "legacy",
    }),
  );
  try {
    const expandBtn = await waitFor(() => {
      const btn = document.querySelector(".ant-table-row-expand-icon");
      assert.ok(btn);
      return btn;
    });
    await act(async () => {
      expandBtn.click();
    });
    await waitFor(() =>
      assert.match(document.body.textContent, /旧版费用（综合费率）/),
    );
    assert.doesNotMatch(document.body.textContent, /综合费用/);
    assert.doesNotMatch(document.body.textContent, /券商佣金|印花税|过户费/);
  } finally {
    await closeLegacy();
  }

  // 3. Unknown costMode (missing execution_costs): retains all rows with known 0 -> 0.00 and missing -> —
  tradesPayload = {
    total: 1,
    items: [
      {
        date: "2025-03-04",
        instrument: "600000.SH",
        name: "浦发银行",
        side: "buy",
        amount: 1000,
        price: 10.5,
        value: 10500,
        cost: 10.5,
        cost_breakdown: {
          impact: 0,
          total: 10.5,
        },
      },
    ],
  };
  const closeUnknown = await mount(
    React.createElement(panel.TradesPanel, {
      runId: "run-trades",
      costMode: undefined,
    }),
  );
  try {
    const expandBtn = await waitFor(() => {
      const btn = document.querySelector(".ant-table-row-expand-icon");
      assert.ok(btn);
      return btn;
    });
    await act(async () => {
      expandBtn.click();
    });
    await waitFor(() =>
      assert.match(document.body.textContent, /旧版费用（综合费率）/),
    );
    const expandedText =
      document.querySelector(".ant-table-expanded-row")?.textContent ?? "";
    assert.match(expandedText, /冲击成本.*0\.00/);
    assert.match(expandedText, /旧版费用（综合费率）.*—/);
  } finally {
    await closeUnknown();
  }
});

after(() => {
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
