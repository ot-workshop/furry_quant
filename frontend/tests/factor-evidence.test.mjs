import assert from "node:assert/strict";
import { test } from "node:test";
import { load } from "./helpers/load.mjs";

const { charts } = await load({ charts: "./src/research/chart-options" });

const palette = {
  text: "#eee",
  muted: "#aaa",
  border: "#333",
  split: "#222",
  surface: "#141414",
  primary: "#5b8cff",
  series: ["#5b8cff", "#aaa", "#a58bff", "#ffa940"],
};

test("horizontalIntervalOption renders interval segment, mean point, and zero reference", () => {
  const option = charts.horizontalIntervalOption(-0.064883, -0.102766, -0.027, palette);
  assert.equal(option.xAxis.type, "value");
  assert.ok(option.xAxis.min <= -0.102766);
  assert.ok(option.xAxis.max >= 0);
  assert.equal(option.series.length, 2);
  assert.equal(option.series[0].type, "line");
  assert.deepEqual(option.series[0].data, [
    [-0.102766, "Rank IC"],
    [-0.027, "Rank IC"],
  ]);
  assert.deepEqual(option.series[0].markLine.data, [{ xAxis: 0 }]);
  assert.equal(option.series[1].type, "scatter");
  assert.deepEqual(option.series[1].data, [[-0.064883, "Rank IC"]]);
});
