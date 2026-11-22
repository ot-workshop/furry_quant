import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { load } from "./helpers/load.mjs";

const { hook } = await load({ hook: "./src/hooks/useExperimentSubmission" });
const React = await import("react");
const { act } = React;
const { createRoot } = await import("react-dom/client");

// The transport intentionally ignores AbortSignal so late fulfillments and
// rejections reach the hook, independently of what native fetch usually does.
test("retired comparison requests cannot clear newer state, report old errors, or navigate", async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: "http://localhost" });
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const fetchBefore = globalThis.fetch;
  const calls = [];
  globalThis.fetch = (url, init) => new Promise((resolve, reject) => {
    calls.push({ url, init, resolve, reject });
  });
  const root = createRoot(document.getElementById("root"));
  const accepted = [];
  let submission;
  function Probe({ active }) {
    submission = hook.useExperimentSubmission("comparison-test", (task) => accepted.push(task.id), active);
    return null;
  }
  const render = (active) => act(async () => root.render(React.createElement(Probe, { active })));
  const submit = (name) => { void submission.submit({ name }); };
  const finish = (index, id) => calls[index].resolve(Response.json({ id }, { status: 202 }));
  try {
    await render(true);
    await act(async () => { submit("A"); submit("A"); });
    assert.equal(calls.length, 1);
    await act(async () => submission.stopWaiting());
    assert.equal(calls[0].init.signal.aborted, true);
    assert.equal(submission.state.kind, "unconfirmed");
    await act(async () => submit("A"));
    await act(async () => finish(0, "obsolete-success"));
    assert.equal(submission.state.kind, "waiting");
    assert.deepEqual(accepted, []);
    assert.equal(calls[0].init.headers["Idempotency-Key"], calls[1].init.headers["Idempotency-Key"]);
    await render(false);
    assert.equal(calls[1].init.signal.aborted, true);
    await render(true);
    await act(async () => submit("B"));
    await act(async () => calls[1].reject(new Error("obsolete failure")));
    assert.equal(submission.state.kind, "waiting");
    assert.notEqual(calls[1].init.headers["Idempotency-Key"], calls[2].init.headers["Idempotency-Key"]);
    await act(async () => finish(2, "latest"));
    assert.deepEqual(accepted, ["latest"]);
    await act(async () => submit("A"));
    assert.equal(calls[3].init.headers["Idempotency-Key"], calls[0].init.headers["Idempotency-Key"]);
  } finally {
    await act(async () => root.unmount());
    if (calls[3]) {
      assert.equal(calls[3].init.signal.aborted, true);
      await act(async () => finish(3, "after-unmount"));
      assert.deepEqual(accepted, ["latest"]);
    }
    globalThis.fetch = fetchBefore;
    dom.window.close();
  }
});
