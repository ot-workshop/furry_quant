import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';

// Load the production chart and registration, then render via ECharts SVG SSR.
const project = fileURLToPath(new URL('../', import.meta.url));
const bundle = await build({stdin: {contents: 'export {annualReturnsOption} from "./src/components/AnnualReturnsChart"; export {init, use} from "echarts/core"; export {SVGRenderer} from "echarts/renderers";', resolveDir: project, loader: 'tsx'},
  bundle: true, platform: 'node', format: 'cjs', packages: 'external', write: false, jsx: 'automatic'});
const module = {exports:{}};
new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(createRequire(new URL('../package.json', import.meta.url)), module, module.exports);
const {annualReturnsOption, init, use, SVGRenderer} = module.exports;

test('production annual bars render as SVG with signed returns and missing benchmark preserved', () => {
  const items = [{year:2025, start_date:'2025-03-03', end_date:'2025-12-31', trading_days:207, strategy_return:.1, benchmark_return:.05},
    {year:2026, start_date:'2026-01-05', end_date:'2026-09-30', trading_days:181, strategy_return:-.05, benchmark_return:null}];
  const option = annualReturnsOption(items);
  assert.deepEqual(option.xAxis.data, ['2025','2026']);
  assert.deepEqual(option.series[1].data, [.05,null]);
  assert.equal(option.tooltip.valueFormatter(null), '—');
  use([SVGRenderer]);
  const chart = init(null, undefined, {renderer:'svg',ssr:true,width:720,height:350});
  try {
    chart.setOption({...option,animation:false});
    const svg = chart.renderToSVGString();
    assert.ok(svg.includes('2025') && svg.includes('2026'));
    assert.ok(svg.includes('10.0%') && svg.includes('-5.0%'));
    assert.ok(svg.includes('#27ae60') && svg.includes('#e74c3c'));
  } finally { chart.dispose(); }
});
