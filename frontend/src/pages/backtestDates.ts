export function positionDates(
  items: Array<{ date: string }>,
  range: { start_date: string; end_date: string },
) {
  // The first equity point can be the previous-session valuation anchor. Only
  // that pre-start point is presentation metadata, not a tradable position date.
  const hasAnchor = Boolean(items[0] && items[0].date < range.start_date);
  return items
    .slice(hasAnchor ? 1 : 0)
    .map((row) => row.date)
    .filter((date) => date >= range.start_date && date <= range.end_date);
}
