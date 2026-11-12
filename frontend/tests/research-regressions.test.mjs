import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";
import { options } from "./fixtures.mjs";

const { form: f } = await load({ form: "./src/research/form" });

for (const frequency of ["daily", "weekly", "monthly"]) {
  for (const window of [10, 30, 60]) {
    test(`pool, window, weights and ${frequency} survive request round trip (${window})`, () => {
      const opts = options();
      let form = f.defaultForm(opts);
      form = {
        ...form, codes: ["600000.SH", "600036.SH"], topk: 1,
        start: "2025-05-01", rebalanceFrequency: frequency,
        costMode: "itemized", commissionRate: 0.05, impactCost: 0.1,
        factors: form.factors.map(row => ({ ...row, weight: row.name === "momentum" ? 70 : row.name === "value" ? 30 : 0, window: row.name === "momentum" ? window : row.window })),
      };
      const built = f.buildRequest(form, opts);
      assert.deepEqual(built.errors, []);
      assert.deepEqual(built.request.data.codes, form.codes);
      assert.equal(built.request.factors[0].window, window);
      assert.equal(built.request.factors[0].weight, 0.7);
      assert.equal(built.request.backtest.rebalance_frequency, frequency);
      assert.equal(built.request.backtest.commission_rate, 0.0005);
      assert.equal(built.request.backtest.impact_cost, 0.001);
      const restored = f.formFromRequest(built.request, opts);
      assert.deepEqual(f.buildRequest(restored.form, opts).request, built.request);
    });
  }
}
