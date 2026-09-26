import type { PageRecord } from "./types";

export type PageSelection = {
  selected_page_ids: string[];
  active_page_id: string | null;
};

export function clearSelection(): PageSelection {
  return { selected_page_ids: [], active_page_id: null };
}

export function singleSelection(pageId: string): PageSelection {
  return { selected_page_ids: [pageId], active_page_id: pageId };
}

function ordered(pages: PageRecord[], ids: Iterable<string>): string[] {
  const wanted = new Set(ids);
  return pages.filter((page) => wanted.has(page.id)).map((page) => page.id);
}

export function ctrlClick(pages: PageRecord[], current: PageSelection, pageId: string): PageSelection {
  const selected = new Set(current.selected_page_ids);
  if (!selected.has(pageId)) {
    selected.add(pageId);
    return { selected_page_ids: ordered(pages, selected), active_page_id: pageId };
  }
  if (pageId !== current.active_page_id) {
    selected.delete(pageId);
    return { selected_page_ids: ordered(pages, selected), active_page_id: current.active_page_id };
  }
  selected.delete(pageId);
  if (selected.size === 0) {
    return clearSelection();
  }
  const index = pages.findIndex((page) => page.id === pageId);
  const after = pages.slice(index + 1).find((page) => selected.has(page.id));
  const before = pages.slice(0, Math.max(index, 0)).reverse().find((page) => selected.has(page.id));
  const next = after ?? before;
  return {
    selected_page_ids: ordered(pages, selected),
    active_page_id: next ? next.id : null,
  };
}

export function shiftClick(pages: PageRecord[], current: PageSelection, pageId: string): PageSelection {
  if (!current.active_page_id) {
    return singleSelection(pageId);
  }
  const start = pages.findIndex((page) => page.id === current.active_page_id);
  const end = pages.findIndex((page) => page.id === pageId);
  if (start < 0 || end < 0) {
    return singleSelection(pageId);
  }
  const [from, to] = start < end ? [start, end] : [end, start];
  return {
    selected_page_ids: pages.slice(from, to + 1).map((page) => page.id),
    active_page_id: pageId,
  };
}

export function selectAll(pages: PageRecord[], current: PageSelection): PageSelection {
  if (pages.length === 0) {
    return clearSelection();
  }
  const ids = pages.map((page) => page.id);
  const active =
    current.active_page_id && ids.includes(current.active_page_id) ? current.active_page_id : ids[0];
  return { selected_page_ids: ids, active_page_id: active };
}

export function moveBy(pages: PageRecord[], activeId: string, delta: number): PageSelection | null {
  const index = pages.findIndex((page) => page.id === activeId);
  const next = index + delta;
  if (index < 0 || next < 0 || next >= pages.length) {
    return null;
  }
  return singleSelection(pages[next].id);
}

export function extendBy(
  pages: PageRecord[],
  anchorId: string,
  movingId: string,
  delta: number,
): PageSelection | null {
  const anchor = pages.findIndex((page) => page.id === anchorId);
  const moving = pages.findIndex((page) => page.id === movingId);
  const next = moving + delta;
  if (anchor < 0 || moving < 0 || next < 0 || next >= pages.length) {
    return null;
  }
  const [from, to] = anchor < next ? [anchor, next] : [next, anchor];
  return {
    selected_page_ids: pages.slice(from, to + 1).map((page) => page.id),
    active_page_id: pages[next].id,
  };
}

export function selectionForPageNumber(pages: PageRecord[], raw: string): PageSelection | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) {
    return null;
  }
  const page = pages.find((item) => item.order === Number(text));
  if (!page) {
    return null;
  }
  return singleSelection(page.id);
}
