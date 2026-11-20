import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";

const { dates, format } = await load({
  dates: "./src/pages/backtestDates",
  format: "./src/utils/format",
});

test("only a first equity point before start_date is excluded from position dates", () => {
  const items = [
    { date: "2025-02-28" },
    { date: "2025-03-03" },
    { date: "2025-03-04" },
  ];
  assert.deepEqual(
    dates.positionDates(items, {
      start_date: "2025-03-03",
      end_date: "2025-03-04",
    }),
    ["2025-03-03", "2025-03-04"],
  );
  // Legacy result: the first point equals start_date and may lack cash/account fields.
  assert.deepEqual(
    dates.positionDates(items.slice(1), {
      start_date: "2025-03-03",
      end_date: "2025-03-04",
    }),
    ["2025-03-03", "2025-03-04"],
  );
  // Empty position days inside the run interval remain selectable.
  assert.deepEqual(
    dates.positionDates(
      [
        { date: "2025-02-28" },
        { date: "2025-03-01" },
        { date: "2025-03-03" },
        { date: "2025-03-04" },
      ],
      { start_date: "2025-03-03", end_date: "2025-03-04" },
    ),
    ["2025-03-03", "2025-03-04"],
  );
});

test("missing impact cost is not displayed as a real zero", () => {
  assert.equal(format.formatPercent(undefined, 3), "—");
  assert.equal(format.formatPercent(null, 3), "—");
  assert.equal(format.formatPercent(0, 3), "0.000%");
  assert.equal(format.formatPercent(0.0005, 3), "0.050%");
});
