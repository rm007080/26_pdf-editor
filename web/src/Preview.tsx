import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { isCancelledRender, paintFittedPage } from "./pdf";
import type { PageRecord } from "./types";

export function visiblePages(pages: PageRecord[]): PageRecord[] {
  return pages.filter((page) => !page.deleted).sort((left, right) => left.order - right.order);
}

export function FittedCanvas({
  pdf,
  sourcePage,
  className,
}: {
  pdf: PDFDocumentProxy;
  sourcePage: number;
  className: string;
}) {
  const frameRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const frame = frameRef.current;
    const canvas = canvasRef.current;
    if (!frame || !canvas) {
      return;
    }
    let cancelled = false;
    let task: { cancel: () => void; promise: Promise<void> } | null = null;

    const draw = () => {
      const width = frame.clientWidth;
      const height = frame.clientHeight;
      if (width < 2 || height < 2) {
        return;
      }
      task?.cancel();
      void paintFittedPage(pdf, sourcePage, canvas, width, height)
        .then((renderTask) => {
          task = renderTask;
          return renderTask.promise;
        })
        .catch((error: unknown) => {
          if (!cancelled && !isCancelledRender(error)) {
            throw error;
          }
        });
    };

    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(frame);
    return () => {
      cancelled = true;
      task?.cancel();
      observer.disconnect();
    };
  }, [pdf, sourcePage]);

  return (
    <div className={className} ref={frameRef}>
      <canvas ref={canvasRef} />
    </div>
  );
}

export function ThumbnailStrip({
  pdf,
  pages,
  names,
  currentPage,
  onSelect,
}: {
  pdf: PDFDocumentProxy;
  pages: PageRecord[];
  names: Map<string, string>;
  currentPage: number;
  onSelect: (order: number) => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const [root, setRoot] = useState<HTMLDivElement | null>(null);

  useEffect(() => {
    setRoot(rootRef.current);
  }, []);

  return (
    <div className="thumbs" ref={rootRef}>
      {pages.map((page) => (
        <Thumbnail
          key={page.id}
          pdf={pdf}
          page={page}
          filename={names.get(page.document_id) ?? ""}
          active={page.order === currentPage}
          root={root}
          onSelect={onSelect}
        />
      ))}
    </div>
  );
}

function Thumbnail({
  pdf,
  page,
  filename,
  active,
  root,
  onSelect,
}: {
  pdf: PDFDocumentProxy;
  page: PageRecord;
  filename: string;
  active: boolean;
  root: HTMLDivElement | null;
  onSelect: (order: number) => void;
}) {
  const itemRef = useRef<HTMLButtonElement>(null);
  const [shown, setShown] = useState(false);

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

  return (
    <button
      ref={itemRef}
      type="button"
      className={active ? "thumb active" : "thumb"}
      onClick={() => onSelect(page.order)}
    >
      {shown ? (
        <FittedCanvas pdf={pdf} sourcePage={page.source_page} className="thumb-frame" />
      ) : (
        <div className="thumb-frame" />
      )}
      <span>現在ページ番号 {page.order}</span>
      <span title={filename}>元 PDF 名 {filename}</span>
      <span>元ページ番号 {page.source_page}</span>
    </button>
  );
}
