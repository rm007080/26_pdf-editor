import { createRoot } from "react-dom/client";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { ZoomPreview } from "../src/Preview";
import type { PageRecord } from "../src/types";
import type { NamedZoom, ZoomMetrics } from "../src/zoom";
import "../src/styles.css";

// PDF の取得時間に依存させず、実際の描画・寸法監視の連鎖をブラウザで検証する。
// スクロールバーが領域の幅を使うブラウザ設定でも実行すること。
const pause = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));
const fixture = document.getElementById("fixture")!;
const results = document.getElementById("results")!;
const root = createRoot(fixture);
const page: PageRecord = {
  id: "page", document_id: "document", source_page: 1, order: 1, deleted: false, rotation: 0,
};

function samplePdf(width: number, height: number): PDFDocumentProxy {
  return {
    getPage: async () => ({
      rotate: 0,
      getViewport: ({ scale, rotation }: { scale: number; rotation: number }) => ({
        width: (rotation % 180 === 0 ? width : height) * scale,
        height: (rotation % 180 === 0 ? height : width) * scale,
      }),
      render: ({ canvas }: { canvas: HTMLCanvasElement }) => {
        let cancelled = false;
        return {
          cancel: () => { cancelled = true; },
          promise: pause(24).then(() => {
            if (cancelled) {
              throw Object.assign(new Error("cancelled"), { name: "RenderingCancelledException" });
            }
            const context = canvas.getContext("2d")!;
            context.fillStyle = "#ffffff";
            context.fillRect(0, 0, canvas.width, canvas.height);
            context.fillStyle = "#243028";
            context.font = "24px sans-serif";
            context.fillText("PDF preview", 24, 40);
          }),
        };
      },
    }),
  } as unknown as PDFDocumentProxy;
}

async function check(name: string, mode: NamedZoom, width: number, height: number) {
  const metrics: ZoomMetrics[] = [];
  const pdfs = new Map([["document", samplePdf(width, height)]]);
  root.render(<ZoomPreview key={name} pdfs={pdfs} pages={[page]} activeId={page.id}
    zoom={{ kind: "named", mode }} onMetrics={(value) => { if (value) metrics.push(value); }} />);
  await pause(400);
  const values: number[] = [];
  let blanks = 0;
  let horizontalOverflow = 0;
  for (let i = 0; i < 40; i++) {
    const viewport = fixture.querySelector<HTMLElement>(".preview-viewport")!;
    if (!fixture.querySelector("canvas")) blanks++;
    if (viewport.scrollWidth > viewport.clientWidth + 1) horizontalOverflow++;
    if (metrics.length) values.push(metrics[metrics.length - 1].effective);
    await pause(25);
  }
  const drift = values.length ? Math.max(...values) - Math.min(...values) : Infinity;
  const pass = values.length === 40 && drift < 0.01 && blanks === 0 && horizontalOverflow === 0;
  const detail = `${name}: ${pass ? "PASS" : "FAIL"} (倍率の変動 ${drift.toFixed(2)}, 空白 ${blanks}/40, 横のはみ出し ${horizontalOverflow}/40)`;
  return { pass, detail };
}

try {
  const checks = [];
  checks.push(await check("横長PDF・幅に合わせる", "fit-width", 960, 540));
  checks.push(await check("縦長PDF・幅に合わせる", "fit-width", 595, 842));
  checks.push(await check("ページ全体", "fit-page", 960, 540));
  const passed = checks.every((check) => check.pass);
  results.dataset.result = passed ? "pass" : "fail";
  results.textContent = `${passed ? "PASS" : "FAIL"}\n${checks.map((check) => check.detail).join("\n")}`;
} catch (error) {
  results.dataset.result = "fail";
  results.textContent = `FAIL\n${String(error)}`;
}
