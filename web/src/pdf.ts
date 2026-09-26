import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";

GlobalWorkerOptions.workerSrc = workerUrl;

export function openSessionPdf(token: string): Promise<PDFDocumentProxy> {
  return getDocument({
    url: "/api/session/file",
    httpHeaders: { Authorization: `Bearer ${token}` },
    disableStream: true,
    disableAutoFetch: true,
  }).promise;
}

export async function paintFittedPage(
  pdf: PDFDocumentProxy,
  sourcePage: number,
  canvas: HTMLCanvasElement,
  maxWidth: number,
  maxHeight: number,
): Promise<RenderTask> {
  const page = await pdf.getPage(sourcePage);
  const base = page.getViewport({ scale: 1 });
  const scale = Math.min(maxWidth / base.width, maxHeight / base.height);
  const viewport = page.getViewport({ scale: Math.max(scale, 0.05) });
  canvas.width = Math.max(1, Math.floor(viewport.width));
  canvas.height = Math.max(1, Math.floor(viewport.height));
  return page.render({ canvas, viewport });
}

export async function paintThumbnail(
  pdf: PDFDocumentProxy,
  sourcePage: number,
  canvas: HTMLCanvasElement,
  cssWidth: number,
  cssHeight: number,
): Promise<RenderTask> {
  const page = await pdf.getPage(sourcePage);
  const dpr = window.devicePixelRatio || 1;
  let pixelWidth = cssWidth * dpr;
  let pixelHeight = cssHeight * dpr;
  const longSide = Math.max(pixelWidth, pixelHeight);
  if (longSide > 400) {
    const factor = 400 / longSide;
    pixelWidth *= factor;
    pixelHeight *= factor;
  }
  canvas.width = Math.max(1, Math.floor(pixelWidth));
  canvas.height = Math.max(1, Math.floor(pixelHeight));
  canvas.style.width = `${cssWidth}px`;
  canvas.style.height = `${cssHeight}px`;
  const base = page.getViewport({ scale: 1 });
  const scale = Math.min(canvas.width / base.width, canvas.height / base.height);
  const viewport = page.getViewport({ scale });
  return page.render({ canvas, viewport });
}

export async function startPageBitmap(
  pdf: PDFDocumentProxy,
  sourcePage: number,
  scale: number,
): Promise<{ canvas: HTMLCanvasElement; task: RenderTask }> {
  const page = await pdf.getPage(sourcePage);
  const safe = scale > 0 ? scale : 0.01;
  const dpr = window.devicePixelRatio || 1;
  const cssViewport = page.getViewport({ scale: safe });
  const pixelViewport = page.getViewport({ scale: safe * dpr });
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.floor(pixelViewport.width));
  canvas.height = Math.max(1, Math.floor(pixelViewport.height));
  canvas.style.width = `${cssViewport.width}px`;
  canvas.style.height = `${cssViewport.height}px`;
  const task = page.render({ canvas, viewport: pixelViewport });
  return { canvas, task };
}

export function isCancelledRender(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name: string }).name === "RenderingCancelledException"
  );
}
