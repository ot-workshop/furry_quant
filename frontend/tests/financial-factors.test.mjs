import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";
import { options } from "./fixtures.mjs";

const { form: f } = await load({ form: "./src/research/form" });

function withCoverage(covered, total = 3) {
  const opts = options();
  return {
    ...opts,
    snapshots: opts.snapshots.map((item) => ({
      ...item,
      financial_coverage: { covered, total },
    })),
  };
}

test("financial coverage comes from the selected dataset", () => {
  const opts = withCoverage(2);
  const form = f.defaultForm(opts);
  assert.deepEqual(f.financialCoverage(form, opts), { covered: 2, total: 3 });
  const noFinancials = options();
  noFinancials.financial_data = null;
  noFinancials.snapshots = noFinancials.snapshots.map((item) => ({ ...item, financial_coverage: null }));
  assert.equal(f.financialCoverage(f.defaultForm(noFinancials), noFinancials), null);
});

test("fundamental factors are only selectable when statements cover the pool", () => {
  const definition = options().factor_definitions.find((item) => item.name === "roe_ttm");
  assert.equal(f.factorAvailable(definition, { covered: 2, total: 3 }, true), true);
  assert.equal(f.factorAvailable(definition, { covered: 1, total: 3 }, true), false);
  assert.equal(f.factorAvailable(definition, null, true), false);
  const price = options().factor_definitions[0];
  assert.equal(f.factorAvailable(price, null, false), true);
});

test("earnings yield and gross margin need a version even when missing stock scores can be filled", () => {
  for (const name of ["value", "gross_margin_ttm"]) {
    const definition = { name, requires_financials: true };
    assert.equal(f.factorAvailable(definition, null, false), false);
    assert.equal(f.factorAvailable(definition, { covered: 3, total: 3 }, false), false);
    assert.equal(f.factorAvailable(definition, { covered: 1, total: 3 }, true), true);
  }
});

test("a new form with no financial version starts with a submit-ready price-only default", () => {
  const opts = { ...options(), financial_data: null };
  const built = f.buildRequest(f.defaultForm(opts), opts);
  assert.deepEqual(built.errors, []);
  assert.deepEqual(built.request.factors.map(({ name, weight }) => [name, weight]), [["momentum", 1]]);
});

test("requests name no financial version: the server pins it when it validates", () => {
  const opts = options();
  const form = f.defaultForm(opts);
  form.factors = form.factors.map((row) =>
    row.name === "roe_ttm" ? { ...row, enabled: true, weight: 50 } : { ...row, weight: 25 },
  );
  const built = f.buildRequest(form, opts);
  assert.deepEqual(built.errors, []);
  assert.equal("financials" in built.request, false);
  assert.deepEqual(
    built.request.factors.map((item) => item.name).sort(),
    ["momentum", "roe_ttm", "value"],
  );
});

test("enabling a fundamental factor without coverage is reported, not silently dropped", () => {
  const opts = withCoverage(0);
  const form = f.defaultForm(opts);
  form.factors = form.factors.map((row) =>
    row.name === "roe_ttm" ? { ...row, enabled: true, weight: 50 } : { ...row, weight: 25 },
  );
  assert.match(f.buildRequest(form, opts).errors.join(), /没有财务数据，不能使用基本面因子/);
});

test("switching to a dataset without statements turns fundamental factors off", () => {
  const opts = withCoverage(0);
  const form = {
    ...f.defaultForm(opts),
    factors: f.defaultForm(opts).factors.map((row) =>
      row.name === "roe_ttm" ? { ...row, enabled: true, weight: 100 } : { ...row, enabled: false, weight: 0 },
    ),
  };
  const pruned = f.pruneFactors(form, opts);
  assert.equal(pruned.factors.find((row) => row.name === "roe_ttm").enabled, false);
  assert.equal(pruned.factors.find((row) => row.name === "roe_ttm").weight, 0);
});

test("a missing financial version preserves the saved factor selection and weights", () => {
  const opts = { ...withCoverage(0), financial_data: null };
  const defaults = f.defaultForm(opts);
  const weights = new Map([["momentum", 60], ["roe_ttm", 40]]);
  const draft = {
    factors: defaults.factors.map((row) => ({
      ...row,
      enabled: weights.has(row.name),
      weight: weights.get(row.name) ?? 0,
    })),
  };
  const reopened = f.pruneFactors(f.mergeDraft(defaults, draft, opts), opts);
  assert.deepEqual(
    reopened.factors.filter((row) => row.enabled).map(({ name, weight }) => [name, weight]),
    [["momentum", 60], ["roe_ttm", 40]],
  );
  const blocked = f.buildRequest(reopened, opts);
  assert.equal(blocked.request, null);
  assert.deepEqual(blocked.errors, [`净资产收益率暂不能使用。${f.FINANCIAL_VERSION_REQUIRED}`]);
  const restoredOptions = withCoverage(2);
  const restored = f.pruneFactors(reopened, restoredOptions);
  assert.deepEqual(
    f.buildRequest(restored, restoredOptions).request.factors.map(({ name, weight }) => [name, weight]),
    [["momentum", 0.6], ["roe_ttm", 0.4]],
  );
});

test("price-only datasets keep every price factor selectable", () => {
  const opts = withCoverage(0);
  const form = f.defaultForm(opts);
  const pruned = f.pruneFactors(form, opts);
  assert.deepEqual(
    pruned.factors.filter((row) => row.enabled).map((row) => row.name),
    ["momentum", "value"],
  );
});

test("new requests and legacy drafts no longer select a financial time mode", () => {
  const opts = withCoverage(3);
  let form = f.defaultForm(opts);
  form.factors = form.factors.map(row => ({ ...row, enabled: row.name === "roe_ttm", weight: row.name === "roe_ttm" ? 100 : 0 }));
  assert.equal("financialPolicy" in form, false);
  const request = f.buildRequest(form, opts).request;
  assert.equal("financial_policy" in request, false);
  assert.equal("financialPolicy" in f.formFromRequest({ ...request, financial_policy: "observed" }, opts).form, false);
  assert.equal("financialPolicy" in f.mergeDraft(form, { financialPolicy: "observed" }, opts), false);
});
