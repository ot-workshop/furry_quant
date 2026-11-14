import type { SelectionDraft } from "../api/factorSelection";
import type { ResearchOptions } from "../api/research";
import { formFromRequest } from "./form";

/** Keep the source financial and industry versions when creating a comparison. */
export function selectionOptions(
  options: ResearchOptions,
  draft: SelectionDraft,
): ResearchOptions {
  const source = draft.research.data;
  const id =
    source.mode === "snapshot" ? source.snapshot_id : source.warehouse_id;
  const merged: ResearchOptions = {
    ...options,
    financial_data: draft.financial_data,
    industry_data: draft.industry_data,
    snapshots: options.snapshots.map((row) =>
      row.id === id
        ? { ...row, financial_coverage: draft.financial_coverage }
        : row,
    ),
    historical_indices: options.historical_indices?.map((row) =>
      row.id === id
        ? { ...row, financial_coverage: draft.financial_coverage }
        : row,
    ),
    market_universes: options.market_universes?.map((row) =>
      row.id === id
        ? { ...row, financial_coverage: draft.financial_coverage }
        : row,
    ),
  };
  const restored = formFromRequest(draft.research, merged);
  if (restored.warnings.length) throw new Error(restored.warnings.join(" "));
  return merged;
}
