import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const dom = new JSDOM('<div id="root"></div>', {
  url: "http://127.0.0.1:8000/backtests/ui-shell",
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
  "location",
  "history",
]) {
  Object.defineProperty(globalThis, name, {
    value: name === "window" ? dom.window : dom.window[name],
    configurable: true,
    writable: true,
  });
}
const computed = dom.window.getComputedStyle.bind(dom.window);
dom.window.getComputedStyle = (element) => computed(element);
globalThis.getComputedStyle = dom.window.getComputedStyle;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

// Layout is driven by media queries: emulate a resizable viewport for them.
const media = { viewport: 390, listeners: new Set() };
const matchesQuery = (query) => {
  const max = /\(max-width:\s*(\d+)px\)/.exec(query);
  const min = /\(min-width:\s*(\d+)px\)/.exec(query);
  let matches = true;
  if (max) matches = matches && media.viewport <= Number(max[1]);
  if (min) matches = matches && media.viewport >= Number(min[1]);
  if (/prefers-color-scheme/.test(query)) matches = false;
  return matches;
};
dom.window.matchMedia = (query) => ({
  media: query,
  get matches() {
    return matchesQuery(query);
  },
  onchange: null,
  addEventListener(type, handler) {
    if (type === "change") media.listeners.add(handler);
  },
  removeEventListener(type, handler) {
    if (type === "change") media.listeners.delete(handler);
  },
  addListener(handler) {
    media.listeners.add(handler);
  },
  removeListener(handler) {
    media.listeners.delete(handler);
  },
  dispatchEvent() {
    return true;
  },
});
Object.defineProperty(dom.window, "innerWidth", {
  get: () => media.viewport,
  configurable: true,
});
function setViewport(width) {
  media.viewport = width;
  for (const handler of [...media.listeners]) handler({ matches: true });
}

globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};
dom.window.ResizeObserver = globalThis.ResizeObserver;
dom.window.scrollTo = () => {};
globalThis.requestAnimationFrame = (callback) => setTimeout(callback, 0);
globalThis.cancelAnimationFrame = (id) => clearTimeout(id);

globalThis.fetch = async (url) => {
  const path = String(url).replace("/api/v1", "");
  if (path.startsWith("/research/tasks"))
    return new Response(JSON.stringify({ items: [], total: 0 }), {
      headers: { "Content-Type": "application/json" },
    });
  throw Error(`Unexpected request ${path}`);
};

const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");
const { ConfigProvider, App } = await import("antd");
const { shell, routes } = await load({
  shell: "./src/app/AppShell",
  routes: "./src/app/routes",
});

const MENU_NAMES = ["工作台", "新建研究", "方案比较", "任务中心", "回测", "数据管理"];

async function waitFor(check) {
  const start = Date.now();
  for (;;) {
    try {
      return check();
    } catch (error) {
      if (Date.now() - start > 5000) throw error;
      await act(() => new Promise((resolve) => setTimeout(resolve, 20)));
    }
  }
}

async function mount() {
  const root = createRoot(document.getElementById("root"));
  await act(async () =>
    root.render(
      React.createElement(
        ConfigProvider,
        null,
        React.createElement(
          App,
          null,
          React.createElement(
            shell.AppShell,
            null,
            React.createElement("div", null, "内容区"),
          ),
        ),
      ),
    ),
  );
  return root;
}

const drawerOpen = () =>
  [...document.querySelectorAll(".ant-drawer")].some((node) =>
    node.className.includes("ant-drawer-open"),
  );

const drawerText = () => {
  const drawer = document.querySelector(".ant-drawer");
  return drawer ? drawer.textContent : null;
};

test("a phone-width shell swaps the icon rail for a menu drawer", async () => {
  setViewport(390);
  history.replaceState(null, "", "/backtests/ui-shell");
  const root = await mount();
  try {
    assert.equal(document.querySelector(".app-sider"), null);
    const toggle = await waitFor(() => {
      const button = document.querySelector('[data-testid="nav-toggle"]');
      assert.ok(button);
      assert.equal(button.getAttribute("aria-label"), "打开导航");
      return button;
    });
    await act(async () => toggle.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await waitFor(() => assert.ok(drawerOpen()));
    const text = drawerText();
    for (const name of MENU_NAMES) assert.ok(text.includes(name), `${name} is listed`);

    const item = [...document.querySelectorAll(".ant-drawer .ant-menu-item")].find(
      (node) => node.textContent.includes("回测"),
    );
    assert.ok(item);
    await act(async () => item.dispatchEvent(new MouseEvent("click", { bubbles: true })));
    await waitFor(() => assert.equal(location.pathname, "/backtests"));
    await waitFor(() => assert.ok(!drawerOpen(), "navigating closes the drawer"));
  } finally {
    await act(async () => root.unmount());
  }
});

test("Escape closes the drawer", async () => {
  setViewport(390);
  history.replaceState(null, "", "/backtests/ui-shell");
  const root = await mount();
  try {
    const toggle = await waitFor(() => {
      const button = document.querySelector('[data-testid="nav-toggle"]');
      assert.ok(button);
      return button;
    });
    await act(async () =>
      toggle.dispatchEvent(new MouseEvent("click", { bubbles: true })),
    );
    await waitFor(() => assert.ok(drawerOpen()));
    await act(async () => {
      document.dispatchEvent(
        new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true }),
      );
    });
    // jsdom never fires transitionend, so focus restoration (AntD's
    // focusTriggerAfterClose) is checked in the real browser instead.
    await waitFor(() => assert.ok(!drawerOpen(), "Escape closes the drawer"));
  } finally {
    await act(async () => root.unmount());
  }
});

test("the desktop shell keeps the sider and crosses the 768px breakpoint", async () => {
  setViewport(1440);
  history.replaceState(null, "", "/backtests/ui-shell");
  const root = await mount();
  try {
    await waitFor(() => assert.ok(document.querySelector(".app-sider")));
    assert.equal(document.querySelector('[data-testid="nav-toggle"]'), null);
    const railText = document.querySelector(".app-sider").textContent;
    for (const name of ["工作台", "方案比较", "回测", "数据管理"])
      assert.ok(railText.includes(name), `${name} stays in the sider`);

    await act(async () => setViewport(767));
    await waitFor(() => assert.equal(document.querySelector(".app-sider"), null));
    assert.ok(document.querySelector('[data-testid="nav-toggle"]'));

    await act(async () => setViewport(768));
    await waitFor(() => assert.ok(document.querySelector(".app-sider")));
    assert.equal(document.querySelector('[data-testid="nav-toggle"]'), null);
    assert.equal(routes.parseRoute("/backtests/ui-shell").section, "backtests");
  } finally {
    await act(async () => root.unmount());
  }
});

// antd's menu/drawer widgets keep an internal MessagePort open, so this worker
// never drains its event loop on its own. `npm test` runs with
// --test-force-exit, which ends the worker after every test has reported.
