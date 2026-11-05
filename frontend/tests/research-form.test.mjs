import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";
import { options } from "./fixtures.mjs";

const { form: f } = await load({ form: "./src/research/form" });

test("defaults build a valid request with percent fields converted to fractions", () => {
  const form = f.defaultForm(options());
  const built = f.buildRequest(form, options());
  assert.deepEqual(built.errors, []);
  assert.deepEqual(built.request.data, { mode: "snapshot", snapshot_id: "shared-a", codes: ["600000.SH", "000001.SZ", "600036.SH"] });
  assert.equal(built.request.backtest.risk_degree, 0.9);
  assert.equal(built.request.backtest.impact_cost, 0.0005);
  assert.deepEqual(
    built.request.factors.map((item) => [item.name, item.weight, item.window]),
    [["momentum", 0.5, 20], ["value", 0.5, null]],
  );
});

test("weights must total 100% and equal weighting fixes it", () => {
  const opts = options();
  let form = f.defaultForm(opts);
  form = { ...form, factors: form.factors.map((row) => (row.name === "volatility" ? { ...row, enabled: true, weight: 10 } : row)) };
  assert.match(f.buildRequest(form, opts).errors.join(), /合计需为 100%，当前为 110%/);
  form = f.equalWeights(form);
  assert.equal(f.weightTotal(form), 100);
  assert.deepEqual(f.buildRequest(form, opts).errors, []);
});

test("warm-up boundary is derived from trading dates and enforced", () => {
  const opts = options();
  const form = { ...f.defaultForm(opts), start: "2025-01-06" };
  const earliest = f.earliestStart(form, opts);
  // momentum(20) needs 21 sessions of history before the first decision.
  assert.equal(earliest, opts.snapshots[0].trading_dates[21]);
  assert.match(f.buildRequest(form, opts).errors.join(), new RegExp(`最早可从 ${earliest}`));
  assert.deepEqual(f.buildRequest({ ...form, start: earliest }, opts).errors, []);
});

test("non-trading dates, too few stocks and oversized topk are rejected", () => {
  const opts = options();
  const base = f.defaultForm(opts);
  assert.match(f.buildRequest({ ...base, end: "2025-06-29" }, opts).errors.join(), /交易日/);
  assert.match(f.buildRequest({ ...base, codes: ["600000.SH"] }, opts).errors.join(), /至少需要 2 只/);
  assert.match(f.buildRequest({ ...base, topk: 4 }, opts).errors.join(), /不能超过股票池数量/);
});

test("extra factor instances get distinct ids and can be removed", () => {
  const opts = options();
  let form = f.addFactorInstance(f.defaultForm(opts), "momentum");
  const copy = form.factors.find((row) => row.key === "momentum_2");
  assert.ok(copy && copy.enabled && copy.weight === 0);
  form = { ...form, factors: form.factors.map((row) => (row.key === "momentum_2" ? { ...row, window: 30, weight: 20 } : row.key === "value" ? { ...row, weight: 30 } : row)) };
  const built = f.buildRequest(form, opts);
  assert.deepEqual(built.errors, []);
  assert.deepEqual(built.request.factors.find((item) => item.id === "momentum_2"), { name: "momentum", weight: 0.2, window: 30, id: "momentum_2" });
  assert.equal(f.removeFactorInstance(form, "momentum_2").factors.some((row) => row.key === "momentum_2"), false);
  assert.equal(f.removeFactorInstance(form, "momentum").factors.some((row) => row.key === "momentum"), true, "base rows stay");
});

test("restoring a request reports codes that disappeared from the dataset", () => {
  const opts = options();
  const request = f.buildRequest(f.defaultForm(opts), opts).request;
  const restored = f.formFromRequest({ ...request, data: { ...request.data, codes: [...request.data.codes, "000858.SZ"] } }, opts);
  assert.deepEqual(restored.form.codes, request.data.codes);
  assert.match(restored.warnings.join(), /已从股票池移除/);
  const missing = f.formFromRequest({ ...request, data: { ...request.data, snapshot_id: "gone" } }, opts);
  assert.match(missing.warnings.join(), /已不可用/);
});

test("drafts merge onto fresh defaults and drop stale values", () => {
  const opts = options();
  const base = f.defaultForm(opts);
  const merged = f.mergeDraft(base, { name: "草稿", snapshotId: "gone", topk: "x", factors: [{ key: "bogus", name: "bogus" }] }, opts);
  assert.equal(merged.name, "草稿");
  assert.equal(merged.snapshotId, base.snapshotId);
  assert.equal(merged.topk, base.topk);
  assert.ok(merged.factors.every((row) => row.name !== "bogus"));
});
