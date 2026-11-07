import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";
import { options } from "./fixtures.mjs";

const { form: f } = await load({ form: "./src/research/form" });

function industryForm(opts, level = "L2") {
  return {
    ...f.defaultForm(opts),
    neutralization: "industry",
    industryVersion: opts.industry_data.id,
    industryLevel: level,
  };
}

test("available industry levels come from the published version", () => {
  const opts = options();
  assert.deepEqual(f.industryLevels(opts), ["L1", "L2", "L3"]);
  assert.equal(f.defaultIndustryLevel(opts), "L1");
  const partial = options();
  partial.industry_data = {
    ...partial.industry_data,
    levels: { L2: partial.industry_data.levels.L2 },
  };
  assert.deepEqual(f.industryLevels(partial), ["L2"]);
  assert.equal(f.defaultIndustryLevel(partial), "L2");
});

test("levels above L1 are sent explicitly and L1 keeps the old request shape", () => {
  const opts = options();
  const l2 = f.buildRequest(industryForm(opts, "L2"), opts);
  assert.deepEqual(l2.errors, []);
  assert.equal(l2.request.neutralization.industry_level, "L2");
  assert.equal(l2.request.neutralization.industry_version, opts.industry_data.id);

  const l1 = f.buildRequest(industryForm(opts, "L1"), opts);
  assert.deepEqual(l1.errors, []);
  assert.equal("industry_level" in l1.request.neutralization, false);
});

test("a missing level is reported before submission", () => {
  const opts = options();
  opts.industry_data = {
    ...opts.industry_data,
    levels: { L1: opts.industry_data.levels.L1 },
  };
  const built = f.buildRequest(industryForm(opts, "L3"), opts);
  assert.match(built.errors.join(), /不包含三级行业/);
});

test("restoring a request keeps its level and bad drafts fall back to L1", () => {
  const opts = options();
  const request = f.buildRequest(industryForm(opts, "L2"), opts).request;
  const restored = f.formFromRequest(request, opts);
  assert.equal(restored.form.industryLevel, "L2");

  const merged = f.mergeDraft(f.defaultForm(opts), { industryLevel: "L9" }, opts);
  assert.equal(merged.industryLevel, "L1");
});
