import { useEffect, useLayoutEffect, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { isCancelledRender, paintThumbnail, startPageBitmap } from "./pdf";
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
  pdf,
  pages,
  activeId,
  zoom,
  onMetrics,
}: {
  pdf: PDFDocumentProxy;
  pages: PageRecord[];
  activeId: string;
  zoom: ZoomState;
  onMetrics: (metrics: ZoomMetrics | null) => void;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const slotRef = useRef<HTMLDivElement>(null);
  const cacheRef = useRef<Map<string, { scale: number; canvas: HTMLCanvasElement }>>(new Map());
  const ratios = useRef({ x: 0, y: 0 });
  const restoring = useRef(false);
  const pdfRef = useRef(pdf);
  const [view, setView] = useState({ width: 0, height: 0 });
  const [base, setBase] = useState<{ width: number; height: number } | null>(null);
  const [tick, setTick] = useState(0);
  const active = pages.find((page) => page.id === activeId) ?? null;

  if (pdfRef.current !== pdf) {
    pdfRef.current = pdf;
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
    if (!active) {
      return;
    }
    const sourcePage = active.source_page;
    let cancelled = false;
    setBase(null);
    void pdf.getPage(sourcePage).then((page) => {
      const viewport = page.getViewport({ scale: 1 });
      if (!cancelled) {
        setBase({ width: viewport.width, height: viewport.height });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [pdf, activeId, active?.source_page]);

  const measured = Boolean(base) && view.width > 1 && view.height > 1;
  const fitPage = measured && base ? Math.min(view.width / base.width, view.height / base.height) * 100 : 0;
  const fitWidth = measured && base ? (view.width / base.width) * 100 : 0;
  const effective = measured ? effectivePercent(zoom, fitPage, fitWidth) : 0;
  const pagesRef = useRef(pages);
  pagesRef.current = pages;
  const neighborKey = active
    ? neighborPages(pages, active.id)
        .map((page) => `${page.id}:${page.source_page}`)
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
      if (!targets.some((page) => page.id === id) || Math.abs(entry.scale - scale) > 0.0001) {
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
        const started = await startPageBitmap(pdf, page.source_page, scale);
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
        cache.set(page.id, { scale, canvas: started.canvas });
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
  }, [pdf, activeId, effective, neighborKey, measured]);

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

export function ThumbnailStrip({
  pdf,
  pages,
  names,
  selectedIds,
  activeId,
  layout,
  frameHeight,
  onActivate,
  onBlank,
  onColumns,
}: {
  pdf: PDFDocumentProxy;
  pages: PageRecord[];
  names: Map<string, string>;
  selectedIds: string[];
  activeId: string | null;
  layout: ThumbnailLayout;
  frameHeight: number;
  onActivate: (pageId: string, mode: "single" | "toggle" | "range") => void;
  onBlank: () => void;
  onColumns: (count: number) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);

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
      {pages.map((page) => (
        <Thumbnail
          key={page.id}
          pdf={pdf}
          page={page}
          filename={names.get(page.document_id) ?? ""}
          selected={selectedIds.includes(page.id)}
          current={page.id === activeId}
          frameHeight={frameHeight}
          root={root}
          onActivate={onActivate}
        />
      ))}
    </div>
  );
}

function Thumbnail({
  pdf,
  page,
  filename,
  selected,
  current,
  frameHeight,
  root,
  onActivate,
}: {
  pdf: PDFDocumentProxy;
  page: PageRecord;
  filename: string;
  selected: boolean;
  current: boolean;
  frameHeight: number;
  root: HTMLDivElement | null;
  onActivate: (pageId: string, mode: "single" | "toggle" | "range") => void;
}) {
  const itemRef = useRef<HTMLButtonElement>(null);
  const [shown, setShown] = useState(false);
  const [ratio, setRatio] = useState(0.75);

  useEffect(() => {
    let cancelled = false;
    void pdf.getPage(page.source_page).then((pdfPage) => {
      const viewport = pdfPage.getViewport({ scale: 1 });
      if (!cancelled && viewport.height > 0) {
        setRatio(viewport.width / viewport.height);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [pdf, page.source_page]);

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
  const className = ["thumb", selected ? "selected" : "", current ? "current" : ""].filter(Boolean).join(" ");

  return (
    <button
      ref={itemRef}
      type="button"
      className={className}
      style={{ width }}
      aria-pressed={selected}
      aria-current={current ? "true" : undefined}
      onContextMenu={(event) => event.preventDefault()}
      onClick={(event) => {
        if (event.shiftKey) {
          onActivate(page.id, "range");
          return;
        }
        if (event.ctrlKey) {
          onActivate(page.id, "toggle");
          return;
        }
        onActivate(page.id, "single");
      }}
    >
      <span className="thumb-frame" style={{ width, height: frameHeight }}>
        {shown ? <ThumbnailCanvas pdf={pdf} sourcePage={page.source_page} width={width} height={frameHeight} /> : null}
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
  );
}

function ThumbnailCanvas({
  pdf,
  sourcePage,
  width,
  height,
}: {
  pdf: PDFDocumentProxy;
  sourcePage: number;
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
    void paintThumbnail(pdf, sourcePage, canvas, width, height)
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
  }, [pdf, sourcePage, width, height]);

  return <canvas ref={canvasRef} />;
}
