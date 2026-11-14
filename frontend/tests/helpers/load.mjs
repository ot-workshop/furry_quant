import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build } from "esbuild";

const project = path.dirname(fileURLToPath(new URL("../../package.json", import.meta.url)));
const localRequire = createRequire(new URL("../../package.json", import.meta.url));

/** Bundle TypeScript modules for Node; charts are replaced because jsdom has no canvas. */
export async function load(entries) {
  const contents = Object.entries(entries)
    .map(([name, file]) => `export * as ${name} from "${file}";`)
    .join("\n");
  const bundled = await build({
    stdin: { contents, resolveDir: project, loader: "tsx" },
    bundle: true,
    platform: "node",
    format: "cjs",
    packages: "external",
    write: false,
    jsx: "automatic",
    loader: { ".css": "empty" },
    plugins: [
      {
        name: "stub-charts",
        setup(builder) {
          builder.onResolve({ filter: /(?:components\/(?:research-)?charts|\.\/charts)$/ }, () => ({ path: "charts", namespace: "stub" }));
          builder.onLoad({ filter: /.*/, namespace: "stub" }, () => ({
            contents: ["EquityChart", "DrawdownChart", "AnnualReturnsChart", "FactorHistoryChart", "GroupReturnsChart", "Chart", "ICSeriesChart", "CorrelationHeatmap", "CorrelationHistoryChart"]
              .map((name) => `export function ${name}(){ return null; }`)
              .join("\n"),
            loader: "js",
          }));
        },
      },
    ],
  });
  const module = { exports: {} };
  new Function("require", "module", "exports", bundled.outputFiles[0].text)(localRequire, module, module.exports);
  return module.exports;
}
