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

export function isCancelledRender(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "name" in error &&
    (error as { name: string }).name === "RenderingCancelledException"
  );
}
