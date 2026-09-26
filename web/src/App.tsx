import { useEffect, useMemo, useState } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import { getExportJob, getSession, openSaved, postDocument, postExport, readToken } from "./api";
import { openSessionPdf } from "./pdf";
import { FittedCanvas, ThumbnailStrip, visiblePages } from "./Preview";
import type { ExportJob, SessionState } from "./types";

const stageLabel: Record<ExportJob["stage"], string> = {
  copying: "コピー中",
  verifying: "検証中",
  committing: "確定中",
  completed: "完了",
  failed: "失敗",
};

const emptySession: SessionState = { documents: [], pages: [] };

function fileNameOf(path: string): string {
  const parts = path.split(/[/\\]/);
  return parts[parts.length - 1] || path;
}

function directoryOf(path: string): string {
  const index = Math.max(path.lastIndexOf("/"), path.lastIndexOf("\\"));
  return index >= 0 ? path.slice(0, index) : path;
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

export function App() {
  const token = useMemo(() => readToken(), []);
  const [session, setSession] = useState<SessionState>(emptySession);
  const [currentPage, setCurrentPage] = useState(1);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [waiting, setWaiting] = useState<string | null>(null);
  const [progress, setProgress] = useState<ExportJob | null>(null);
  const [saved, setSaved] = useState<{ jobId: string; path: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const pages = visiblePages(session.pages);
  const names = useMemo(
    () => new Map(session.documents.map((document) => [document.id, document.filename])),
    [session.documents],
  );
  const active = pages.find((page) => page.order === currentPage) ?? null;
  const documentId = session.documents[0]?.id ?? "";

  useEffect(() => {
    let activeRequest = true;
    getSession(token)
      .then((next) => {
        if (!activeRequest) {
          return;
        }
        setSession(next);
        if (visiblePages(next.pages).length > 0) {
          setCurrentPage(1);
        }
      })
      .catch((reason: unknown) => {
        if (activeRequest) {
          setError(reason instanceof Error ? reason.message : "編集状態を読めませんでした");
        }
      });
    return () => {
      activeRequest = false;
    };
  }, [token]);

  useEffect(() => {
    if (!documentId) {
      setPdf(null);
      return;
    }
    let activeRequest = true;
    let loaded: PDFDocumentProxy | null = null;
    openSessionPdf(token)
      .then((document) => {
        if (!activeRequest) {
          void document.destroy();
          return;
        }
        loaded = document;
        setPdf(document);
      })
      .catch((reason: unknown) => {
        if (activeRequest) {
          setError(reason instanceof Error ? reason.message : "PDF を表示できませんでした");
        }
      });
    return () => {
      activeRequest = false;
      void loaded?.destroy();
    };
  }, [token, documentId]);

  async function chooseFile(file: File | undefined) {
    if (!file) {
      return;
    }
    setError(null);
    setSaved(null);
    setProgress(null);
    try {
      const next = await postDocument(token, file);
      setSession(next);
      setCurrentPage(1);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "読み込めませんでした");
    }
  }

  async function openResult(kind: "open-file" | "open-folder") {
    if (!saved) {
      return;
    }
    try {
      await openSaved(token, saved.jobId, kind);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "開けませんでした");
    }
  }

  async function exportAll() {
    setBusy(true);
    setError(null);
    setSaved(null);
    setProgress(null);
    setWaiting("保存先を選択しています");
    try {
      const started = await postExport(token);
      if ("status" in started) {
        setWaiting(null);
        return;
      }
      let job = await getExportJob(token, started.job_id);
      setProgress(job);
      setWaiting(null);
      while (job.stage !== "completed" && job.stage !== "failed") {
        await delay(300);
        job = await getExportJob(token, started.job_id);
        setProgress(job);
      }
      if (job.stage === "failed") {
        setError(job.detail ?? "書き出しに失敗しました");
        return;
      }
      if (job.path) {
        setSaved({ jobId: job.job_id, path: job.path });
      }
    } catch (reason: unknown) {
      setWaiting(null);
      setError(reason instanceof Error ? reason.message : "書き出せませんでした");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="app">
      <header className="toolbar">
        <h1>PDF 編集</h1>
        <label className="file-button">
          PDF を選ぶ
          <input
            type="file"
            accept="application/pdf,.pdf"
            onChange={(event) => {
              const file = event.target.files?.[0];
              event.target.value = "";
              void chooseFile(file);
            }}
          />
        </label>
        <button type="button" disabled={!active || currentPage <= 1} onClick={() => setCurrentPage((page) => page - 1)}>
          前のページ
        </button>
        <button
          type="button"
          disabled={!active || currentPage >= pages.length}
          onClick={() => setCurrentPage((page) => page + 1)}
        >
          次のページ
        </button>
        <button type="button" className="export-button" disabled={pages.length === 0 || busy} onClick={() => void exportAll()}>
          全ページを書き出す
        </button>
        <span className="page-label">{active ? `${currentPage} / ${pages.length}` : "ページなし"}</span>
      </header>
      {error ? (
        <p className="notice">
          {error}
          <button type="button" onClick={() => setError(null)}>
            閉じる
          </button>
        </p>
      ) : null}
      {waiting ? <p className="result">{waiting}</p> : null}
      {progress && progress.stage !== "completed" ? (
        <p className="result">
          {stageLabel[progress.stage]} {progress.processed_pages}/{progress.total_pages} ページ（
          {Math.round(progress.ratio * 100)}%）
        </p>
      ) : null}
      {saved ? (
        <div className="result">
          <span>ファイル名 {fileNameOf(saved.path)}</span>
          <span>保存先 {directoryOf(saved.path)}</span>
          <button type="button" onClick={() => void openResult("open-file")}>
            PDF を開く
          </button>
          <button type="button" onClick={() => void openResult("open-folder")}>
            保存先を開く
          </button>
        </div>
      ) : null}
      <main className="stage">
        {pdf && active ? (
          <FittedCanvas pdf={pdf} sourcePage={active.source_page} className="preview-frame" />
        ) : (
          <p className="empty">PDF を選ぶと、1 ページ目がここに表示されます。</p>
        )}
      </main>
      {pdf && pages.length > 0 ? (
        <ThumbnailStrip
          pdf={pdf}
          pages={pages}
          names={names}
          currentPage={currentPage}
          onSelect={setCurrentPage}
        />
      ) : null}
    </div>
  );
}
