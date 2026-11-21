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
]) {
  Object.defineProperty(globalThis, name, {
    value: name === "window" ? dom.window : dom.window[name],
    configurable: true,
    writable: true,
  });
}
const computedStyle = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computedStyle(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
dom.window.matchMedia = () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
});
globalThis.matchMedia = dom.window.matchMedia;
const trackedChannels = [];
class TrackedMessageChannel extends globalThis.MessageChannel {
  constructor() {
    super();
    trackedChannels.push(this);
  }
}
globalThis.MessageChannel = TrackedMessageChannel;
dom.window.MessageChannel = TrackedMessageChannel;
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
const { App, ConfigProvider } = await import("antd");

const { correlation, charts } = await load({
  correlation: "./src/evaluation/CorrelationAnalysis",
  charts: "./src/research/chart-options",
});

let analysisPayload = null;
const requests = [];

globalThis.fetch = async (url) => {
  const parsed = new URL(String(url), dom.window.location.origin);
  requests.push(parsed);
  const json = (value, status = 200) =>
    new Response(JSON.stringify(value), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  if (parsed.pathname.endsWith("/analysis")) return json(analysisPayload);
  if (parsed.pathname.endsWith("/selection")) {
    return json({
      reference: { evaluation_id: "ev-1", selection_id: "s-1", parameters: {} },
      parameters: {},
      evaluation_id: "ev-1",
      selection_id: "s-1",
      status: "optimal",
      selected_keys: ["momentum"],
      stages: {
        input: 3,
        quality_passed: 2,
        dominance_removed: 0,
        milp_candidates: 1,
      },
      candidates: [],
      methodology: [],
      message: "ok",
    });
  }
  return json({ detail: "unmocked" }, 404);
};

async function mount(Component, props) {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(App, null, React.createElement(Component, props)),
      ),
    ),
  );
  return async () => {
    await act(async () => root.unmount());
  };
}

async function waitFor(check) {
  const started = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - started > 4000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    }
  }
}

const baseResult = {
  metadata: { id: "ev-corr-test", start: "2025-01-02", end: "2025-06-30" },
  factors: {
    momentum: {},
    volatility: {},
    value: {},
    composite: {},
  },
};

test("CorrelationAnalysis announces partial uncomputable factors, preserves real 0, and keeps detail table aligned", async () => {
  analysisPayload = {
    factor: "momentum",
    horizon: 1,
    window: 20,
    min_periods: 20,
    start: null,
    end: null,
    series: [],
    summary: { ic: {}, rank_ic: {} },
    correlations: {
      names: ["momentum", "volatility", "value"],
      cells: [
        {
          first: "momentum",
          second: "momentum",
          value: 1,
          mean_absolute: 1,
          valid_dates: 60,
        },
        {
          first: "momentum",
          second: "volatility",
          value: 0,
          mean_absolute: 0.12,
          valid_dates: 60,
        },
        {
          first: "momentum",
          second: "value",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
        {
          first: "volatility",
          second: "volatility",
          value: 1,
          mean_absolute: 1,
          valid_dates: 60,
        },
        {
          first: "volatility",
          second: "value",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
        {
          first: "value",
          second: "value",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
      ],
      pair: {
        first: "momentum",
        second: "volatility",
        daily: [{ date: "2025-01-03", rank_correlation: 0 }],
      },
      method: "spearman",
    },
    methodology: {},
  };

  const unmount = await mount(correlation.CorrelationAnalysis, {
    result: baseResult,
    horizon: "1",
  });
  try {
    await waitFor(() =>
      assert.match(
        document.body.textContent,
        /3 个因子中 1 个（盈利收益率）无法计算相关性，其行列不参与比较/,
      ),
    );
    assert.match(document.body.textContent, /动量 \/ 低波动00\.1260查看历史/);
    assert.match(
      document.body.textContent,
      /动量 \/ 盈利收益率—无法计算相关系数（有效日期不足，或因子在区间内没有变化）—0不可切换/,
    );
    assert.match(document.body.textContent, /斜纹\/点阵虚线格「—」/);
    assert.doesNotMatch(
      document.body.textContent,
      /当前条件下所有因子均无法计算相关性/,
    );

    const viewBtn = document.querySelector(
      'button[aria-label="查看 动量 与 低波动 历史相关性"]',
    );
    assert.ok(
      viewBtn,
      "Expected keyboard-accessible detail button for valid pair",
    );
    assert.equal(
      document.querySelector(
        'button[aria-label="查看 动量 与 盈利收益率 历史相关性"]',
      ),
      null,
    );
    const reqsBefore = requests.length;
    await act(async () => {
      viewBtn.click();
    });
    await waitFor(() => assert.equal(requests.length, reqsBefore));
    // Clicking "查看 动量 与 动量 历史相关性" actually triggers a new /analysis pair fetch.
    const diagBtn = document.querySelector(
      'button[aria-label="查看 动量 与 动量 历史相关性"]',
    );
    await act(async () => {
      diagBtn.click();
    });
    await waitFor(() =>
      assert.ok(
        requests.some(
          (u) =>
            u.searchParams.get("first") === "momentum" &&
            u.searchParams.get("second") === "momentum",
        ),
      ),
    );

    const stats = charts.correlationMatrixStats(
      analysisPayload.correlations.names,
      analysisPayload.correlations.cells,
    );
    assert.deepEqual(stats.uncomputableFactors, ["value"]);
    assert.equal(stats.allUncomputable, false);
  } finally {
    await unmount();
  }
});

test("CorrelationAnalysis renders explicit empty state when all pairs are uncomputable", async () => {
  analysisPayload = {
    factor: "momentum",
    horizon: 1,
    window: 20,
    min_periods: 20,
    start: null,
    end: null,
    series: [],
    summary: { ic: {}, rank_ic: {} },
    correlations: {
      names: ["momentum", "value"],
      cells: [
        {
          first: "momentum",
          second: "momentum",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
        {
          first: "momentum",
          second: "value",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
        {
          first: "value",
          second: "value",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
      ],
      pair: { first: "momentum", second: "value", daily: [] },
      method: "spearman",
    },
    methodology: {},
  };

  const unmount = await mount(correlation.CorrelationAnalysis, {
    result: {
      metadata: baseResult.metadata,
      factors: { momentum: {}, value: {}, composite: {} },
    },
    horizon: "1",
  });
  try {
    await waitFor(() =>
      assert.match(
        document.body.textContent,
        /当前条件下所有因子均无法计算相关性/,
      ),
    );
    assert.doesNotMatch(document.body.textContent, /斜纹\/点阵虚线格「—」/);
    assert.doesNotMatch(
      document.body.textContent,
      /2 个因子中 2 个.*不参与比较/,
    );
  } finally {
    await unmount();
  }
});

test("CorrelationAnalysis treats daily series of all null correlations as empty state", async () => {
  analysisPayload = {
    factor: "momentum",
    horizon: 1,
    window: 20,
    min_periods: 20,
    start: null,
    end: null,
    series: [],
    summary: { ic: {}, rank_ic: {} },
    correlations: {
      names: ["momentum", "volatility", "value"],
      cells: [
        {
          first: "momentum",
          second: "momentum",
          value: 1,
          mean_absolute: 1,
          valid_dates: 60,
        },
        {
          first: "volatility",
          second: "volatility",
          value: 1,
          mean_absolute: 1,
          valid_dates: 60,
        },
        {
          first: "momentum",
          second: "value",
          value: null,
          mean_absolute: null,
          valid_dates: 0,
        },
      ],
      pair: {
        first: "momentum",
        second: "value",
        daily: [
          { date: "2025-01-03", rank_correlation: null },
          { date: "2025-01-06", rank_correlation: null },
        ],
      },
      method: "spearman",
    },
    methodology: {},
  };

  const unmount = await mount(correlation.CorrelationAnalysis, {
    result: baseResult,
    horizon: "1",
  });
  try {
    await waitFor(() =>
      assert.match(
        document.body.textContent,
        /该因子对在所选范围无法计算相关性/,
      ),
    );
  } finally {
    await unmount();
  }
});

after(() => {
  for (const channel of trackedChannels) {
    channel.port1.close();
    channel.port2.close();
  }
  trackedChannels.length = 0;
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
