export type ReorderAnchor = { before_page_id: string } | { after_page_id: string };

export function reorderAnchor(orderedPageIds: string[], movingIds: string[], slot: number): ReorderAnchor | "same" {
  const moving = new Set(movingIds);
  const movingList = orderedPageIds.filter((pageId) => moving.has(pageId));
  const stationary = orderedPageIds.filter((pageId) => !moving.has(pageId));
  if (movingList.length === 0 || stationary.length === 0) {
    return "same";
  }
  const bounded = Math.max(0, Math.min(slot, orderedPageIds.length));
  let stationaryBefore = 0;
  for (let index = 0; index < bounded; index += 1) {
    if (!moving.has(orderedPageIds[index])) {
      stationaryBefore += 1;
    }
  }
  if (stationaryBefore <= 0) {
    return { before_page_id: stationary[0] };
  }
  if (stationaryBefore >= stationary.length) {
    return { after_page_id: stationary[stationary.length - 1] };
  }
  return { before_page_id: stationary[stationaryBefore] };
}

export function insertionSlot(items: HTMLElement[], point: { x: number; y: number }): number {
  if (items.length === 0) {
    return 0;
  }
  let best = items.length;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (let index = 0; index <= items.length; index += 1) {
    const rect = (index < items.length ? items[index] : items[items.length - 1]).getBoundingClientRect();
    const x = index < items.length ? rect.left : rect.right;
    const y = rect.top + rect.height / 2;
    const distance = (point.x - x) ** 2 + (point.y - y) ** 2;
    if (distance < bestDistance) {
      bestDistance = distance;
      best = index;
    }
  }
  return best;
}
