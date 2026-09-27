import { getDocument, GlobalWorkerOptions, type PDFDocumentProxy, type RenderTask } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { tabId } from "./api";

GlobalWorkerOptions.workerSrc = workerUrl;

export function openDocumentPdf(token: string, documentId: string): Promise<PDFDocumentProxy> {
  return getDocument({
    url: `/api/session/files/${documentId}`,
    httpHeaders: { Authorization: `Bearer ${token}`, "X-Tab-Id": tabId() },
    disableStream: true,
    disableAutoFetch: true,
  }).promise;
}

export function displayedRotation(inherent: number, additional: number): number {
  return (inherent + additional) % 360;
}

export function ratiosAfterClockwise(ratios: { x: number; y: number }, quarterTurns: number): { x: number; y: number } {
  let point = { x: ratios.x, y: ratios.y };
  const turns = ((quarterTurns % 4) + 4) % 4;
  for (let step = 0; step < turns; step += 1) {
    point = { x: 1 - point.y, y: point.x };
  }
  return point;
}

export async function paintFittedPage(
  pdf: PDFDocumentProxy,
  sourcePage: number,
  canvas: HTMLCanvasElement,
  maxWidth: number,
  maxHeight: number,
  additionalRotation = 0,
): Promise<RenderTask> {
  const page = await pdf.getPage(sourcePage);
  const rotation = displayedRotation(page.rotate, additionalRotation);
  const base = page.getViewport({ scale: 1, rotation });
  const scale = Math.min(maxWidth / base.width, maxHeight / base.height);
  const viewport = page.getViewport({ scale: Math.max(scale, 0.05), rotation });
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
  additionalRotation = 0,
): Promise<RenderTask> {
  const page = await pdf.getPage(sourcePage);
  const rotation = displayedRotation(page.rotate, additionalRotation);
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
  const base = page.getViewport({ scale: 1, rotation });
  const scale = Math.min(canvas.width / base.width, canvas.height / base.height);
  const viewport = page.getViewport({ scale, rotation });
  return page.render({ canvas, viewport });
}

export async function startPageBitmap(
  pdf: PDFDocumentProxy,
  sourcePage: number,
  scale: number,
  additionalRotation = 0,
): Promise<{ canvas: HTMLCanvasElement; task: RenderTask }> {
  const page = await pdf.getPage(sourcePage);
  const rotation = displayedRotation(page.rotate, additionalRotation);
  const safe = scale > 0 ? scale : 0.01;
  const dpr = window.devicePixelRatio || 1;
  const cssViewport = page.getViewport({ scale: safe, rotation });
  const pixelViewport = page.getViewport({ scale: safe * dpr, rotation });
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
