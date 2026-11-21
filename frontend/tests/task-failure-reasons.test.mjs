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
const { tasksUi, failureUtil } = await load({
  tasksUi: "./src/components/tasks",
  failureUtil: "./src/utils/taskFailure",
});

function makeTask(overrides = {}) {
  return {
    id: `task-${Math.random().toString(16).slice(2, 10).padEnd(8, "0")}`,
    kind: "research",
    name: "测试任务",
    status: "failed",
    stage: "finished",
    message: "运行失败",
    created_at: "2026-11-21T06:00:00Z",
    started_at: "2026-11-21T06:00:05Z",
    finished_at: "2026-11-21T06:05:05Z",
    progress: { completed: 2, total: 4 },
    request: {
      name: "测试任务",
      data: {
        mode: "snapshot",
        snapshot_id: "snap-1",
        codes: [{ code: "000001.SZ", name: "平安" }],
      },
      factors: [{ name: "momentum", window: 20, weight: 1 }],
      backtest: {
        start: "2024-01-02",
        end: "2024-06-28",
        topk: 5,
        rebalance_frequency: "daily",
        initial_cash: 100000,
      },
    },
    result: null,
    error: null,
    error_details: null,
    ...overrides,
  };
}

async function mount(element) {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(App, null, element),
      ),
    ),
  );
  return async () => act(async () => root.unmount());
}

test("exit codes map -9/137, -15/143, arbitrary codes, OOM proof and unknown stages", () => {
  const sigkillNoProof = failureUtil.describeTaskFailure(
    makeTask({
      error: "在回测阶段被系统强制终止（退出码 -9）",
      error_details: { stage: "backtest", exit_code: -9 },
    }),
  );
  assert.equal(sigkillNoProof.headline, "回测阶段 · 被系统强制终止");
  assert.equal(sigkillNoProof.exitCodeText, "退出码 -9");
  assert.equal(sigkillNoProof.fullText.includes("内存不足"), false);

  const shell137Proof = failureUtil.describeTaskFailure(
    makeTask({
      error: "在回测阶段因内存不足被系统终止（退出码 137）",
      error_details: { stage: "backtest", exit_code: 137, oom_killed: true },
    }),
  );
  assert.equal(shell137Proof.headline, "回测阶段 · 因内存不足被系统终止");
  assert.equal(shell137Proof.exitCodeText, "退出码 137");
  assert.equal(shell137Proof.fullText.includes("超出容器配额"), false);

  const unknownStage = failureUtil.describeTaskFailure(
    makeTask({
      error: "任务进程退出，代码 -9，请查看任务日志",
      error_details: null,
    }),
  );
  assert.equal(unknownStage.headline, "被系统强制终止");
  assert.equal(unknownStage.headline.startsWith("·"), false);
  assert.equal(unknownStage.fullText, "被系统强制终止 · 退出码 -9");

  const sigterm = failureUtil.describeTaskFailure(
    makeTask({
      status: "interrupted",
      error: "在回测阶段被要求停止（退出码 -15）",
      error_details: { stage: "backtest", exit_code: -15 },
    }),
  );
  assert.equal(sigterm.headline, "回测阶段 · 被要求停止");
  assert.equal(sigterm.tone, "neutral");

  const shell143 = failureUtil.describeTaskFailure(
    makeTask({
      status: "interrupted",
      error: "被要求停止（退出码 143）",
      error_details: { stage: null, exit_code: 143 },
    }),
  );
  assert.equal(shell143.headline, "被要求停止");
  assert.equal(shell143.tone, "neutral");

  const winCode = failureUtil.describeTaskFailure(
    makeTask({
      error: "异常退出（退出码 3221225477）",
      error_details: { stage: "factors", exit_code: 3221225477 },
    }),
  );
  assert.equal(winCode.headline, "计算因子阶段 · 异常退出");
  assert.equal(winCode.exitCodeText, "退出码 3221225477");
  assert.equal(winCode.fullText.includes("内存不足"), false);
});

test("TaskTable displays inline failure reasons without expanding and keeps compact & detail wording aligned", async () => {
  const longReason =
    "回测执行过程中子进程标准错误输出超过常规摘要长度，用于验证折叠截断后以省略号收尾并允许键盘聚焦查看完整说明且不发生逐字竖排或容器裁切：" +
    "详细异常发生在第 1627 步截面聚合计算阶段，原始上下文信息完整保留在这里用于核对展开后的全部文本是否完全可读。";
  const items = [
    makeTask({
      id: "task-a",
      name: "强杀带阶段",
      error: "在回测阶段被系统强制终止（退出码 -9）",
      error_details: { stage: "backtest", exit_code: -9, oom_killed: false },
    }),
    makeTask({
      id: "task-b",
      name: "普通非零码",
      error: "在计算因子阶段异常退出（退出码 1），请查看任务日志",
      error_details: { stage: "factors", exit_code: 1 },
    }),
    makeTask({
      id: "task-c",
      name: "空原因任务",
      error: null,
      error_details: null,
    }),
    makeTask({
      id: "task-d",
      name: "超长原因任务",
      error: longReason,
      error_details: { stage: "backtest" },
    }),
    makeTask({
      id: "task-e",
      name: "无记录阶段任务",
      error: "任务进程退出，代码 -9，请查看任务日志",
      error_details: null,
    }),
    makeTask({
      id: "task-f",
      name: "停止任务",
      status: "interrupted",
      message: "运行中断",
      error: "在回测阶段被要求停止（退出码 -15）",
      error_details: { stage: "backtest", exit_code: -15 },
    }),
  ];

  // Compact mode (Dashboard): not expandable, still shows inline reasons and expandable long text
  const closeCompact = await mount(
    React.createElement(tasksUi.TaskTable, {
      tasks: items,
      onChanged: () => {},
      compact: true,
    }),
  );
  try {
    const rowA = document.querySelector('[data-task-id="task-a"]');
    assert.ok(rowA.classList.contains("task-row-failed"));
    assert.match(
      rowA.textContent,
      /原因：回测阶段 · 被系统强制终止 · ，退出码 -9/,
    );

    const rowB = document.querySelector('[data-task-id="task-b"]');
    assert.match(rowB.textContent, /计算因子阶段 · 异常退出/);
    assert.match(rowB.textContent, /退出码 1/);
    assert.equal(rowB.textContent.includes("内存不足"), false);

    const rowC = document.querySelector('[data-task-id="task-c"]');
    assert.ok(rowC.querySelector(".task-reason-label"));
    assert.match(rowC.textContent, /未记录原因/);
    assert.ok(rowC.querySelector(".task-reason-empty"));

    const rowE = document.querySelector('[data-task-id="task-e"]');
    const rowEReason = rowE.querySelector(".task-row-reason").textContent;
    assert.match(rowEReason, /被系统强制终止/);
    assert.equal(rowEReason.startsWith("·"), false);
    assert.equal(rowEReason.includes("阶段未知"), false);
    assert.equal(rowEReason.includes("内存不足"), false);

    const rowF = document.querySelector('[data-task-id="task-f"]');
    assert.equal(rowF.classList.contains("task-row-failed"), false);
    assert.match(rowF.textContent, /回测阶段 · 被要求停止/);

    const rowD = document.querySelector('[data-task-id="task-d"]');
    const toggle = rowD.querySelector("button.task-reason-toggle");
    assert.ok(toggle);
    assert.equal(toggle.getAttribute("aria-expanded"), "false");
    assert.ok(rowD.querySelector(".task-reason-clamped"));
    await act(async () => toggle.click());
    assert.equal(toggle.getAttribute("aria-expanded"), "true");
    assert.equal(rowD.querySelector(".task-reason-clamped"), null);
    assert.equal(rowD.textContent.includes("完全可读"), true);
  } finally {
    await closeCompact();
  }

  // Full mode (TasksPage): expanding detail shows matching reason copy
  const closeFull = await mount(
    React.createElement(tasksUi.TaskTable, {
      tasks: [items[0]],
      onChanged: () => {},
    }),
  );
  try {
    const detailBtn = [...document.querySelectorAll("button")].find((b) =>
      b.textContent.includes("详情"),
    );
    await act(async () => detailBtn.click());
    const detail = document.querySelector(".task-detail-reason");
    assert.ok(detail);
    assert.match(detail.textContent, /回测阶段 · 被系统强制终止 · 退出码 -9/);
  } finally {
    await closeFull();
  }
});

after(() => {
  dom.window.close();
  setTimeout(() => process.exit(), 200).unref();
});
