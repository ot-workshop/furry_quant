import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";

const { page } = await load({ page: "./src/pages/EvaluationDetailPage" });

const row = (date, exclusion = "insufficient_cross_section") => ({
  date,
  entry_date: date,
  exit_date: date,
  observations: 2,
  ic: null,
  rank_ic: null,
  ic_exclusion: exclusion,
  group_returns: { 1: null, 2: null, 3: null },
  group_counts: { 1: 0, 2: 0, 3: 0 },
  high_minus_low: null,
});

test("a complete run of insufficient cross-section days names the reason", () => {
  const reason = page.groupEmptyReason(
    [row("2025-02-03"), row("2025-02-04")],
    2,
    3,
  );
  assert.match(reason, /同时具备有效评分和未来收益标签的样本数/);
  assert.match(reason, /3 只/);
});

test("a run whose days do not cover the summary falls back to the generic hint", () => {
  // One stored day, but the summary says the factor had 415 signal dates.
  assert.equal(page.groupEmptyReason([row("2025-02-03")], 415, 3), null);
  assert.equal(page.groupEmptyReason([row("2025-02-03")], undefined, 3), null);
});

test("duplicate dates are not treated as complete evidence", () => {
  assert.equal(
    page.groupEmptyReason([row("2025-02-03"), row("2025-02-03")], 2, 3),
    null,
  );
});

test("constant_score is named only on that evidence and mixed days stay generic", () => {
  const constant = page.groupEmptyReason(
    [row("2025-02-03", "constant_score"), row("2025-02-04", "constant_score")],
    2,
    3,
  );
  assert.match(constant, /有收益标签的配对样本/);
  assert.equal(
    page.groupEmptyReason(
      [
        row("2025-02-03", "constant_score"),
        row("2025-02-04", "insufficient_cross_section"),
      ],
      2,
      3,
    ),
    null,
  );
});

test("missing daily or a row without a date falls back instead of claiming a cause", () => {
  assert.equal(page.groupEmptyReason(undefined, 2, 3), null);
  assert.equal(page.groupEmptyReason(null, 2, 3), null);
  assert.equal(page.groupEmptyReason([], 2, 3), null);
  const withoutDate = [
    row("2025-02-03"),
    { ...row("2025-02-04"), date: undefined },
  ];
  assert.equal(page.groupEmptyReason(withoutDate, 2, 3), null);
  const emptyDate = [row("2025-02-03"), { ...row("2025-02-04"), date: "" }];
  assert.equal(page.groupEmptyReason(emptyDate, 2, 3), null);
});
