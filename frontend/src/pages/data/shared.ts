import { type MarketScope } from "../../api/data";
export const SCOPES: Record<MarketScope, string> = {
  custom: "自选股票",
  csi300: "沪深300历史成分",
  all_market: "全市场（沪深 A 股）",
};
export const CROSS_STATUS: Record<string, string> = {
  verified: "双源核验通过",
  fallback: "部分回退",
  conflict: "存在冲突",
};
export const VALIDATION: Record<string, string> = {
  single_source_passed: "单源检查",
  dual_source_verified: "双源核验",
  secondary_fallback_passed: "补缺通过",
};
