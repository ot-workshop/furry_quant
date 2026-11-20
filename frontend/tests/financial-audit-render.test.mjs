import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { load } from "./helpers/load.mjs";

const { audit } = await load({ audit: "./src/components/audit" });

test("financial audit renders missing bindings and unknown evidence in Chinese", () => {
  for (const [status, label] of [["missing_binding", "未绑定"], ["unknown", "无法判断"]]) {
    const html = renderToStaticMarkup(React.createElement(audit.TemporalAuditPanel, {
      audit: {
        items: [{ key: "financials", label: "财务报表", status, detail: "依赖与绑定说明" }],
      },
    }));
    assert.ok(html.includes(label));
    assert.ok(html.includes("依赖与绑定说明"));
    assert.ok(!html.includes(`>${status}<`));
  }
});
