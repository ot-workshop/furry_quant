import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { build } from "esbuild";
import React, { act } from "react";

export async function createDOM({ url = "http://127.0.0.1:8000" } = {}) {
  const dom = new JSDOM('<div id="root"></div>', { url, pretendToBeVisual: true });
  for (const name of ["window", "document", "navigator", "HTMLElement", "HTMLInputElement", "HTMLSelectElement", "Node", "Event", "MouseEvent"])
    Object.defineProperty(globalThis, name, { value: name === "window" ? dom.window : dom.window[name], configurable: true });
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  dom.window.scrollTo = () => {};
  const { createRoot } = await import("react-dom/client");
  const project = path.dirname(fileURLToPath(new URL("../../package.json", import.meta.url)));
  const localRequire = createRequire(new URL("../../package.json", import.meta.url));
  const bundled = await build({
    stdin: { contents: 'export {default as App} from "./src/App"; export {ResearchPage} from "./src/pages/ResearchPage"; export {BacktestPage} from "./src/pages/BacktestPage"; export {EvaluationPage} from "./src/pages/EvaluationPage"; export {StockFactorsPage} from "./src/pages/StockFactorsPage"; export {IndexDownloadPanel} from "./src/components/IndexDownloadPanel"; export {TemporalAuditPanel} from "./src/components/TemporalAuditPanel"; export {SharedMarketPanel} from "./src/components/SharedMarketPanel";', resolveDir: project, loader: "tsx" },
    bundle: true, platform: "node", format: "cjs", packages: "external", write: false, jsx: "automatic", loader: { ".css": "empty" },
    plugins: [{ name: "omit-canvas-charts", setup(builder) {
      builder.onResolve({ filter: /\/(EquityChart|DrawdownChart|FactorHistoryChart|AnnualReturnsChart)$/ }, args => ({ path: args.path.split("/").at(-1), namespace: "test-chart" }));
      builder.onLoad({ filter: /.*/, namespace: "test-chart" }, args => ({ contents: args.path === "FactorHistoryChart"
        ? 'import React from "react"; export function FactorHistoryChart({items}) { return React.createElement("div", {"data-testid":"factor-history", "data-dates":JSON.stringify(items.map(row=>row.date))}); }'
        : args.path === 'AnnualReturnsChart'
        ? 'import React from "react"; export function AnnualReturnsChart({items}) { return React.createElement("div", {"data-testid":"annual-returns", "data-items":JSON.stringify(items)}); }'
        : `export function ${args.path}(){ return null; }`, loader: "js", resolveDir: project }));
    } }],
  });
  const loaded = { exports: {} };
  new Function("require", "module", "exports", bundled.outputFiles[0].text)(localRequire, loaded, loaded.exports);
  return { dom, components: loaded.exports, React, act, createRoot };
}
