import { Fragment, useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { isCancelledRender, paintThumbnail, ratiosAfterClockwise, startPageBitmap } from "./pdf";
import { insertionSlot, reorderAnchor, type ReorderAnchor } from "./reorder";
import type { PageRecord, ThumbnailLayout } from "./types";
import type { ZoomMetrics, ZoomState } from "./zoom";
import { effectivePercent } from "./zoom";

export function visiblePages(pages: PageRecord[]): PageRecord[] {
  return pages.filter((page) => !page.deleted).sort((left, right) => left.order - right.order);
}

function neighborPages(pages: PageRecord[], activeId: string): PageRecord[] {
  const index = pages.findIndex((page) => page.id === activeId);
  if (index < 0) {
    return [];
  }
  return pages.slice(Math.max(0, index - 1), index + 2);
}

export function ZoomPreview({
  pdfs,
  pages,
  activeId,
  zoom,
  onMetrics,
}: {
  pdfs: Map<string, PDFDocumentProxy>;
  pages: PageRecord[];
  activeId: string;
  zoom: ZoomState;
  onMetrics: (metrics: ZoomMetrics | null) => void;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const slotRef = useRef<HTMLDivElement>(null);
  const cacheRef = useRef<Map<string, { scale: number; rotation: number; canvas: HTMLCanvasElement }>>(new Map());
  const ratios = useRef({ x: 0, y: 0 });
  const restoring = useRef(false);
  const active = pages.find((page) => page.id === activeId) ?? null;
  const activePdf = active ? (pdfs.get(active.document_id) ?? null) : null;
  const pdfRef = useRef<PDFDocumentProxy | null>(activePdf);
  const [view, setView] = useState({ width: 0, height: 0 });
  const [base, setBase] = useState<{ width: number; height: number } | null>(null);
  const [tick, setTick] = useState(0);

  if (pdfRef.current !== activePdf) {
    pdfRef.current = activePdf;
    cacheRef.current.clear();
  }

  useEffect(() => {
    const node = viewportRef.current;
    if (!node) {
      return;
    }
    const update = () => setView({ width: node.clientWidth, height: node.clientHeight });
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!active || !activePdf) {
      return;
    }
    const sourcePage = active.source_page;
    let cancelled = false;
    setBase(null);
    const additional = active.rotation;
    void activePdf.getPage(sourcePage).then((page) => {
      const viewport = page.getViewport({ scale: 1, rotation: (page.rotate + additional) % 360 });
      if (!cancelled) {
        setBase({ width: viewport.width, height: viewport.height });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [activePdf, activeId, active?.source_page, active?.rotation]);

  const measured = Boolean(base) && view.width > 1 && view.height > 1;
  const fitPage = measured && base ? Math.min(view.width / base.width, view.height / base.height) * 100 : 0;
  const fitWidth = measured && base ? (view.width / base.width) * 100 : 0;
  const effective = measured ? effectivePercent(zoom, fitPage, fitWidth) : 0;
  const pagesRef = useRef(pages);
  pagesRef.current = pages;
  const neighborKey = active
    ? neighborPages(pages, active.id)
        .map((page) => `${page.id}:${page.source_page}:${page.rotation}`)
        .join("|")
    : "";

  useEffect(() => {
    if (!measured) {
      onMetrics(null);
      return;
    }
    onMetrics({ effective, fitPage, fitWidth });
  }, [measured, effective, fitPage, fitWidth, onMetrics]);

  useEffect(() => {
    if (!active || !measured) {
      return;
    }
    let cancelled = false;
    const tasks: Array<{ cancel: () => void }> = [];
    const scale = effective / 100;
    const current = active;
    const targets = neighborPages(pagesRef.current, current.id);
    const cache = cacheRef.current;
    for (const [id, entry] of cache) {
      const page = targets.find((item) => item.id === id);
      if (!page || Math.abs(entry.scale - scale) > 0.0001 || entry.rotation !== page.rotation) {
        cache.delete(id);
      }
    }
    if (!cache.has(current.id)) {
      slotRef.current?.replaceChildren();
    }
    void (async () => {
      for (const page of targets) {
        if (cancelled || cache.has(page.id)) {
          continue;
        }
        const pagePdf = pdfs.get(page.document_id);
        if (!pagePdf) {
          continue;
        }
        const started = await startPageBitmap(pagePdf, page.source_page, scale, page.rotation);
        tasks.push(started.task);
        if (cancelled) {
          started.task.cancel();
          return;
        }
        try {
          await started.task.promise;
        } catch (error: unknown) {
          if (isCancelledRender(error) || cancelled) {
            return;
          }
          throw error;
        }
        if (cancelled) {
          return;
        }
        cache.set(page.id, { scale, rotation: page.rotation, canvas: started.canvas });
        while (cache.size > 3) {
          const extra = [...cache.keys()].find((id) => id !== current.id);
          if (!extra) {
            break;
          }
          cache.delete(extra);
        }
        setTick((value) => value + 1);
      }
    })();
    return () => {
      cancelled = true;
      for (const task of tasks) {
        task.cancel();
      }
    };
  }, [pdfs, activeId, effective, neighborKey, measured]);

  useLayoutEffect(() => {
    const slot = slotRef.current;
    if (!slot || !active) {
      return;
    }
    const entry = cacheRef.current.get(active.id);
    slot.replaceChildren();
    if (entry) {
      slot.append(entry.canvas);
    }
  }, [tick, activeId]);

  const rotationWatch = useRef<{ id: string; rotation: number } | null>(null);
  useLayoutEffect(() => {
    if (!active) {
      return;
    }
    const previous = rotationWatch.current;
    if (previous && previous.id === active.id && previous.rotation !== active.rotation) {
      const quarterTurns = ((active.rotation - previous.rotation + 360) % 360) / 90;
      ratios.current = ratiosAfterClockwise(ratios.current, quarterTurns);
    }
    rotationWatch.current = { id: active.id, rotation: active.rotation };
  }, [active]);

  useLayoutEffect(() => {
    const node = viewportRef.current;
    if (!node) {
      return;
    }
    restoring.current = true;
    const maxX = node.scrollWidth - node.clientWidth;
    const maxY = node.scrollHeight - node.clientHeight;
    node.scrollLeft = ratios.current.x * Math.max(maxX, 0);
    node.scrollTop = ratios.current.y * Math.max(maxY, 0);
    restoring.current = false;
  }, [tick, activeId, effective]);

  function remember() {
    if (restoring.current) {
      return;
    }
    const node = viewportRef.current;
    if (!node) {
      return;
    }
    const maxX = node.scrollWidth - node.clientWidth;
    const maxY = node.scrollHeight - node.clientHeight;
    if (maxX > 0) {
      ratios.current.x = node.scrollLeft / maxX;
    }
    if (maxY > 0) {
      ratios.current.y = node.scrollTop / maxY;
    }
  }

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) {
      return;
    }
    const node = viewportRef.current;
    if (!node) {
      return;
    }
    const startX = event.clientX;
    const startY = event.clientY;
    const startLeft = node.scrollLeft;
    const startTop = node.scrollTop;
    const target = event.currentTarget;
    target.setPointerCapture(event.pointerId);
    const move = (ev: PointerEvent) => {
      node.scrollLeft = startLeft - (ev.clientX - startX);
      node.scrollTop = startTop - (ev.clientY - startY);
    };
    const up = () => {
      target.removeEventListener("pointermove", move);
      target.removeEventListener("pointerup", up);
    };
    target.addEventListener("pointermove", move);
    target.addEventListener("pointerup", up);
  }

  return (
    <div
      className="preview-viewport"
      ref={viewportRef}
      onScroll={remember}
      onPointerDown={onPointerDown}
    >
      <div className="preview-slot" ref={slotRef} />
    </div>
  );
}

const DRAG_START_PX = 8;
const AUTO_SCROLL_PX_PER_SECOND = 480;
const AUTO_SCROLL_EDGE_PX = 48;

let cancelActiveDrag: () => boolean = () => false;

export function cancelThumbnailDrag(): boolean {
  return cancelActiveDrag();
}

type ThumbDrag = {
  pointerId: number;
  pageId: string;
  startX: number;
  startY: number;
  active: boolean;
  slot: number;
};

export function ThumbnailStrip({
  pdfs,
  pages,
  names,
  selectedIds,
  activeId,
  layout,
  frameHeight,
  onSelect,
  onRotate,
  onDelete,
  onReorder,
  onUnchangedReorder,
  onBlank,
  onColumns,
  splitPositions,
  onToggleSplit,
  splitDisabled,
  splitReason,
}: {
  pdfs: Map<string, PDFDocumentProxy>;
  pages: PageRecord[];
  names: Map<string, string>;
  selectedIds: string[];
  activeId: string | null;
  layout: ThumbnailLayout;
  frameHeight: number;
  onSelect: (pageId: string, mode: "single" | "toggle" | "range") => void;
  onRotate: (pageId: string, direction: "left" | "right") => void;
  onDelete: (pageId: string) => void;
  onReorder: (pageIds: string[], anchor: ReorderAnchor) => void;
  onUnchangedReorder: () => void;
  onBlank: () => void;
  onColumns: (count: number) => void;
  splitPositions: number[];
  onToggleSplit: (afterPageId: string, present: boolean) => void;
  splitDisabled?: boolean;
  splitReason?: string;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);
  const [dragging, setDragging] = useState(false);
  const [dragSlot, setDragSlot] = useState<number | null>(null);
  const dragRef = useRef<ThumbDrag | null>(null);
  const pointRef = useRef({ x: 0, y: 0 });
  const suppressClick = useRef(false);
  const pagesRef = useRef(pages);
  const selectedRef = useRef(selectedIds);
  pagesRef.current = pages;
  selectedRef.current = selectedIds;

  useEffect(() => {
    setRoot(rootRef.current);
  }, []);

  useEffect(() => {
    const node = rootRef.current;
    if (!node || layout !== "row") {
      return;
    }
    const onWheel = (event: WheelEvent) => {
      if (event.ctrlKey) {
        return;
      }
      const delta = Math.abs(event.deltaY) > Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
      if (delta === 0) {
        return;
      }
      event.preventDefault();
      node.scrollLeft += delta;
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, [layout]);

  useEffect(() => {
    const node = rootRef.current;
    if (!node) {
      return;
    }
    const measure = () => {
      const items = [...node.querySelectorAll<HTMLElement>(".thumb")];
      if (items.length === 0) {
        onColumns(1);
        return;
      }
      const top = items[0].offsetTop;
      const columns = items.filter((item) => Math.abs(item.offsetTop - top) < 4).length;
      onColumns(Math.max(1, columns));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [layout, frameHeight, pages, onColumns]);

  useEffect(() => {
    cancelActiveDrag = () => {
      if (!dragRef.current?.active) {
        return false;
      }
      dragRef.current = null;
      suppressClick.current = true;
      setDragging(false);
      setDragSlot(null);
      return true;
    };
    return () => {
      cancelActiveDrag = () => false;
    };
  }, []);

  useEffect(() => {
    if (!dragging) {
      return;
    }
    let frame = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const node = rootRef.current;
      if (node) {
        const rect = node.getBoundingClientRect();
        const point = pointRef.current;
        const speed = AUTO_SCROLL_PX_PER_SECOND * dt;
        if (layout === "row") {
          if (point.x < rect.left + AUTO_SCROLL_EDGE_PX) {
            node.scrollLeft -= speed;
          } else if (point.x > rect.right - AUTO_SCROLL_EDGE_PX) {
            node.scrollLeft += speed;
          }
        } else if (point.y < rect.top + AUTO_SCROLL_EDGE_PX) {
          node.scrollTop -= speed;
        } else if (point.y > rect.bottom - AUTO_SCROLL_EDGE_PX) {
          node.scrollTop += speed;
        }
      }
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [dragging, layout]);

  function thumbElements(): HTMLElement[] {
    return [...(rootRef.current?.querySelectorAll<HTMLElement>(".thumb") ?? [])];
  }

  function onThumbPointerDown(event: ReactPointerEvent<HTMLButtonElement>, pageId: string) {
    if (event.button !== 0) {
      return;
    }
    dragRef.current = {
      pointerId: event.pointerId,
      pageId,
      startX: event.clientX,
      startY: event.clientY,
      active: false,
      slot: 0,
    };
    pointRef.current = { x: event.clientX, y: event.clientY };
    event.currentTarget.setPointerCapture(event.pointerId);
  }

  function onThumbPointerMove(event: ReactPointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    pointRef.current = { x: event.clientX, y: event.clientY };
    const distance = Math.hypot(event.clientX - drag.startX, event.clientY - drag.startY);
    if (!drag.active && distance < DRAG_START_PX) {
      return;
    }
    if (!drag.active) {
      drag.active = true;
      setDragging(true);
    }
    const next = insertionSlot(thumbElements(), pointRef.current);
    drag.slot = next;
    setDragSlot(next);
  }

  function onThumbPointerUp(event: ReactPointerEvent<HTMLButtonElement>) {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) {
      return;
    }
    dragRef.current = null;
    if (!drag.active) {
      return;
    }
    suppressClick.current = true;
    setDragging(false);
    setDragSlot(null);
    const ordered = pagesRef.current.map((page) => page.id);
    const moving = selectedRef.current.includes(drag.pageId) ? selectedRef.current : [drag.pageId];
    const anchor = reorderAnchor(ordered, moving, drag.slot);
    if (anchor === "same") {
      onUnchangedReorder();
      return;
    }
    onReorder(moving, anchor);
  }

  return (
    <div
      className={layout === "grid" ? "thumbs grid" : "thumbs row"}
      ref={rootRef}
      tabIndex={0}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) {
          onBlank();
        }
      }}
    >
      {pages.map((page, index) => (
        <Fragment key={page.id}>
          {dragSlot === index ? <span className="drop-line" /> : null}
          <Thumbnail
            pdf={pdfs.get(page.document_id) ?? null}
            page={page}
            filename={names.get(page.document_id) ?? ""}
            selected={selectedIds.includes(page.id)}
            active={page.id === activeId}
            frameHeight={frameHeight}
            root={root}
            onSelect={onSelect}
            onRotate={onRotate}
            onDelete={onDelete}
            onPointerDown={onThumbPointerDown}
            onPointerMove={onThumbPointerMove}
            onPointerUp={onThumbPointerUp}
            consumeSuppressedClick={() => {
              if (!suppressClick.current) {
                return false;
              }
              suppressClick.current = false;
              return true;
            }}
          />
          {index < pages.length - 1 ? (
            <SplitMark
              present={splitPositions.includes(index + 1)}
              disabled={splitDisabled}
              reason={splitReason}
              onToggle={() => onToggleSplit(page.id, splitPositions.includes(index + 1))}
            />
          ) : null}
        </Fragment>
      ))}
      {dragSlot === pages.length ? <span className="drop-line" /> : null}
    </div>
  );
}

function SplitMark({
  present,
  disabled,
  reason,
  onToggle,
}: {
  present: boolean;
  disabled?: boolean;
  reason?: string;
  onToggle: () => void;
}) {
  return (
    <button
      type="button"
      className={present ? "split-line on" : "split-line"}
      aria-pressed={present}
      aria-label={present ? "分割線を外す" : "分割線を入れる"}
      disabled={disabled}
      title={disabled ? reason : present ? "この分割線を外します" : "ここに分割線を入れます"}
      onPointerDown={(event) => event.stopPropagation()}
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
        onToggle();
      }}
    />
  );
}

function Thumbnail({
  pdf,
  page,
  filename,
  selected,
  active,
  frameHeight,
  root,
  onSelect,
  onRotate,
  onDelete,
  onPointerDown,
  onPointerMove,
  onPointerUp,
  consumeSuppressedClick,
}: {
  pdf: PDFDocumentProxy | null;
  page: PageRecord;
  filename: string;
  selected: boolean;
  active: boolean;
  frameHeight: number;
  root: HTMLDivElement | null;
  onSelect: (pageId: string, mode: "single" | "toggle" | "range") => void;
  onRotate: (pageId: string, direction: "left" | "right") => void;
  onDelete: (pageId: string) => void;
  onPointerDown: (event: ReactPointerEvent<HTMLButtonElement>, pageId: string) => void;
  onPointerMove: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  onPointerUp: (event: ReactPointerEvent<HTMLButtonElement>) => void;
  consumeSuppressedClick: () => boolean;
}) {
  const itemRef = useRef<HTMLDivElement>(null);
  const [shown, setShown] = useState(false);
  const [ratio, setRatio] = useState(0.75);

  useEffect(() => {
    if (!pdf) {
      return;
    }
    let cancelled = false;
    void pdf.getPage(page.source_page).then((pdfPage) => {
      const viewport = pdfPage.getViewport({
        scale: 1,
        rotation: (pdfPage.rotate + page.rotation) % 360,
      });
      if (!cancelled && viewport.height > 0) {
        setRatio(viewport.width / viewport.height);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [pdf, page.source_page, page.rotation]);

  useEffect(() => {
    const node = itemRef.current;
    if (!node || !root) {
      return;
    }
    const observer = new IntersectionObserver(
      (entries) => {
        setShown(entries.some((entry) => entry.isIntersecting));
      },
      { root },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, [root]);

  const width = Math.max(1, Math.round(frameHeight * ratio));
  const className = ["thumb", selected ? "selected" : "", active ? "active" : ""].filter(Boolean).join(" ");

  return (
    <div ref={itemRef} className={className} style={{ width }}>
      <button
        type="button"
        className="thumb-main"
        aria-pressed={selected}
        aria-current={active ? "true" : undefined}
        onContextMenu={(event) => event.preventDefault()}
        onPointerDown={(event) => onPointerDown(event, page.id)}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onClick={(event) => {
          if (consumeSuppressedClick()) {
            return;
          }
          if (event.shiftKey) {
            onSelect(page.id, "range");
            return;
          }
          if (event.ctrlKey) {
            onSelect(page.id, "toggle");
            return;
          }
          onSelect(page.id, "single");
        }}
      >
        <span className="thumb-frame" style={{ width, height: frameHeight }}>
          {shown && pdf ? (
            <ThumbnailCanvas
              pdf={pdf}
              sourcePage={page.source_page}
              additionalRotation={page.rotation}
              width={width}
              height={frameHeight}
            />
          ) : null}
        </span>
        {selected ? (
          <span className="thumb-check" aria-hidden="true">
            ✓
          </span>
        ) : null}
        <span>現在ページ番号 {page.order}</span>
        <span className="thumb-name" title={filename}>
          元 PDF 名 {filename}
        </span>
        <span>元ページ番号 {page.source_page}</span>
      </button>
      <span className="thumb-actions">
        <button type="button" aria-label="左に回転" onClick={() => onRotate(page.id, "left")}>
          左
        </button>
        <button type="button" aria-label="右に回転" onClick={() => onRotate(page.id, "right")}>
          右
        </button>
        <button type="button" aria-label="このページを削除" onClick={() => onDelete(page.id)}>
          削除
        </button>
      </span>
    </div>
  );
}

function ThumbnailCanvas({
  pdf,
  sourcePage,
  additionalRotation,
  width,
  height,
}: {
  pdf: PDFDocumentProxy;
  sourcePage: number;
  additionalRotation: number;
  width: number;
  height: number;
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || width < 2 || height < 2) {
      return;
    }
    let cancelled = false;
    let task: { cancel: () => void } | null = null;
    void paintThumbnail(pdf, sourcePage, canvas, width, height, additionalRotation)
      .then((renderTask) => {
        task = renderTask;
        return renderTask.promise;
      })
      .catch((error: unknown) => {
        if (!cancelled && !isCancelledRender(error)) {
          throw error;
        }
      });
    return () => {
      cancelled = true;
      task?.cancel();
    };
  }, [pdf, sourcePage, additionalRotation, width, height]);

  return <canvas ref={canvasRef} />;
}
