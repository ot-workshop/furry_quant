import assert from "node:assert/strict";
import { after, test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/tasks",
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
const { tasksUi } = await load({ tasksUi: "./src/components/tasks" });

const sampleTasks = [
  {
    id: "task-succeeded-1",
    kind: "research",
    name: "动量短线策略验证",
    status: "succeeded",
    stage: "finished",
    message: "计算完成",
    created_at: "2026-11-21T08:00:00Z",
    started_at: "2026-11-21T08:00:02Z",
    finished_at: "2026-11-21T08:01:30Z",
    progress: { completed: 4, total: 4 },
    request: {
      name: "动量短线策略验证",
      data: { mode: "market_warehouse" },
      factors: [{ name: "momentum", window: 20, weight: 1 }],
      backtest: {
        start: "2024-01-02",
        end: "2024-06-28",
        topk: 10,
        rebalance_frequency: "daily",
        initial_cash: 1000000,
      },
    },
    result: {
      backtest_run_id: "bt-1",
      factor_run_id: "fr-1",
      evaluation_run_id: "ev-1",
      factor_as_of: "2024-06-28",
    },
    error: null,
    error_details: null,
  },
];

test("TaskTable restores head arrow button, single-toggle row click, keyboard Enter/Space/Escape, and detail isolation", async () => {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          App,
          null,
          React.createElement(tasksUi.TaskTable, {
            tasks: sampleTasks,
            onChanged: () => {},
          }),
        ),
      ),
    ),
  );
  try {
    const row = document.querySelector(".task-row");
    assert.ok(row);
    // Row is focusable listitem, NOT role="button", keeping inner links accessible.
    assert.equal(row.getAttribute("role"), "listitem");
    assert.equal(row.getAttribute("tabindex"), "0");

    const arrowBtn = row.querySelector(".task-row-expand-btn");
    assert.ok(arrowBtn, "Expected visible circular arrow button");
    assert.equal(arrowBtn.tagName, "BUTTON");
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "false");
    const controlsId = arrowBtn.getAttribute("aria-controls");
    assert.equal(controlsId, "task-detail-task-succeeded-1");

    // 1. Clicking arrow toggles ONCE (false -> true -> false).
    await act(async () => arrowBtn.click());
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "true");
    const detail = document.getElementById(controlsId);
    assert.ok(detail, "Expected detail panel matching aria-controls");
    assert.equal(
      detail.querySelector(".task-detail-title")?.textContent,
      sampleTasks[0].name,
    );
    assert.ok(detail.querySelector(".task-detail-steps"));

    await act(async () => arrowBtn.click());
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "false");
    assert.equal(document.getElementById(controlsId), null);

    // 2. Clicking blank spacer / non-link area toggles ONCE (false -> true).
    const spacer = row.querySelector(".task-row-spacer");
    assert.ok(spacer);
    await act(async () =>
      spacer.dispatchEvent(
        new dom.window.MouseEvent("click", { bubbles: true }),
      ),
    );
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "true");

    // 3. Clicking inside detail panel does NOT collapse row.
    await act(async () =>
      document
        .getElementById(controlsId)
        .dispatchEvent(new dom.window.MouseEvent("click", { bubbles: true })),
    );
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "true");

    // 4. Clicking result link does NOT collapse row.
    const resultLink = row.querySelector(".task-row-meta a");
    assert.ok(resultLink);
    await act(async () =>
      resultLink.dispatchEvent(
        new dom.window.MouseEvent("click", { bubbles: true }),
      ),
    );
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "true");

    // 5. Clicking detail action button toggles ONCE (true -> false).
    const detailTextBtn = [
      ...row.querySelectorAll(".task-row-actions button"),
    ].find((b) => b.textContent.includes("详情"));
    assert.ok(detailTextBtn);
    await act(async () => detailTextBtn.click());
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "false");

    // 6. Keyboard Enter / Space / Escape on row.
    await act(async () =>
      row.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", {
          key: "Enter",
          bubbles: true,
        }),
      ),
    );
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "true");

    await act(async () =>
      row.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", {
          key: "Escape",
          bubbles: true,
        }),
      ),
    );
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "false");

    await act(async () =>
      row.dispatchEvent(
        new dom.window.KeyboardEvent("keydown", { key: " ", bubbles: true }),
      ),
    );
    assert.equal(arrowBtn.getAttribute("aria-expanded"), "true");
  } finally {
    await act(async () => root.unmount());
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
