import { useCallback, useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import {
  ApiError,
  discardDocuments,
  getExportJob,
  getSession,
  getSettings,
  openSaved,
  patchSelection,
  postCommand,
  postDocument,
  postExport,
  putSettings,
  readToken,
} from "./api";
import { FRAME_HEIGHT, SPLITTER, oneRowHeight, paneHeights, ratioFromThumb } from "./panes";
import { openDocumentPdf } from "./pdf";
import { ThumbnailStrip, ZoomPreview, cancelThumbnailDrag, visiblePages } from "./Preview";
import {
  clearSelection,
  ctrlClick,
  extendBy,
  moveBy,
  selectAll,
  selectionForPageNumber,
  shiftClick,
  singleSelection,
  type PageSelection,
} from "./selection";
import type { EditCommand, ExportJob, SessionState, ThumbnailLayout, ThumbnailSize, ViewSettings } from "./types";
import type { ReorderAnchor } from "./reorder";
import { changeZoom, formatPercent, initialZoom, zoomUnchanged, type NamedZoom, type ZoomMetrics, type ZoomState } from "./zoom";

const stageLabel: Record<ExportJob["stage"], string> = {
  copying: "コピー中",
  verifying: "検証中",
  committing: "確定中",
  completed: "完了",
  failed: "失敗",
};

const emptySession: SessionState = {
  documents: [],
  pages: [],
  selected_page_ids: [],
  active_page_id: null,
  undo_enabled: false,
  redo_enabled: false,
  undo_hint: "元に戻せる操作がありません。実行しても状態は変わりません",
  redo_hint: "やり直せる操作がありません。実行しても状態は変わりません",
  unsaved: false,
};

const defaultSettings: ViewSettings = {
  thumbnail_layout: "row",
  thumbnail_size: "medium",
  pane_ratio: null,
};

const selectPrompt = "ページを選択してください";
const noPageReason = "ページが選択されていないため使えません。ページを選択すると使えます。";

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

function isTextField(target: EventTarget | null): boolean {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
}

function TipButton({
  label,
  disabled,
  reason,
  title,
  pressed,
  onClick,
}: {
  label: string;
  disabled?: boolean;
  reason?: string;
  title?: string;
  pressed?: boolean;
  onClick: () => void;
}) {
  return (
    <span className="tip" title={title ?? (disabled ? reason : undefined)}>
      <button type="button" disabled={disabled} aria-pressed={pressed} onClick={onClick}>
        {label}
      </button>
    </span>
  );
}

export function App() {
  const token = useMemo(() => readToken(), []);
  const [session, setSession] = useState<SessionState>(emptySession);
  const [settings, setSettings] = useState<ViewSettings>(defaultSettings);
  const [settingsReady, setSettingsReady] = useState(false);
  const [pdfs, setPdfs] = useState<Map<string, PDFDocumentProxy>>(new Map());
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [waiting, setWaiting] = useState<string | null>(null);
  const [progress, setProgress] = useState<ExportJob | null>(null);
  const [saved, setSaved] = useState<{ jobId: string; path: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [jumpText, setJumpText] = useState("");
  const [moveText, setMoveText] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [removedCount, setRemovedCount] = useState<number | null>(null);
  const [zoom, setZoom] = useState<ZoomState>(initialZoom);
  const [metrics, setMetrics] = useState<ZoomMetrics | null>(null);
  const [workspaceHeight, setWorkspaceHeight] = useState(0);
  const [dragRatio, setDragRatio] = useState<number | null>(null);
  const [columns, setColumns] = useState(1);
  const workspaceRef = useRef<HTMLDivElement>(null);
  const thumbsRef = useRef<HTMLElement>(null);
  const anchorRef = useRef<string | null>(null);
  const ticketRef = useRef(0);
  const zoomRef = useRef(zoom);
  const metricsRef = useRef(metrics);
  const sessionRef = useRef(session);
  const settingsRef = useRef(settings);
  const columnsRef = useRef(columns);
  const busyRef = useRef(busy);
  const loadingRef = useRef(false);
  const abortRef = useRef(false);
  const pdfsRef = useRef<Map<string, PDFDocumentProxy>>(new Map());
  const toastTimer = useRef(0);
  zoomRef.current = zoom;
  metricsRef.current = metrics;
  sessionRef.current = session;
  settingsRef.current = settings;
  columnsRef.current = columns;
  busyRef.current = busy;
  loadingRef.current = loading;

  const pages = visiblePages(session.pages);
  const names = useMemo(
    () => new Map(session.documents.map((document) => [document.id, document.filename])),
    [session.documents],
  );
  const active = pages.find((page) => page.id === session.active_page_id) ?? null;
  const activePdf = active ? (pdfs.get(active.document_id) ?? null) : null;
  const documentKey = session.documents.map((document) => document.id).join("\n");
  const frameHeight = FRAME_HEIGHT[settings.thumbnail_size];
  const available = Math.max(0, workspaceHeight - SPLITTER);
  const heights = paneHeights(available, dragRatio ?? settings.pane_ratio, oneRowHeight(settings.thumbnail_size));
  const onMetrics = useCallback((next: ZoomMetrics | null) => {
    setMetrics(next);
  }, []);
  const onColumns = useCallback((count: number) => {
    setColumns(count);
  }, []);

  useEffect(() => {
    let activeRequest = true;
    getSession(token)
      .then((next) => {
        if (!activeRequest) {
          return;
        }
        setSession(next);
        anchorRef.current = next.active_page_id;
      })
      .catch((reason: unknown) => {
        if (activeRequest) {
          setError(reason instanceof Error ? reason.message : "編集状態を読めませんでした");
        }
      });
    getSettings(token)
      .then((next) => {
        if (activeRequest) {
          setSettings(next);
        }
      })
      .catch((reason: unknown) => {
        if (activeRequest) {
          setError(reason instanceof Error ? reason.message : "設定を読めませんでした");
        }
      })
      .finally(() => {
        if (activeRequest) {
          setSettingsReady(true);
        }
      });
    return () => {
      activeRequest = false;
    };
  }, [token]);

  useEffect(() => {
    const node = workspaceRef.current;
    if (!node) {
      return;
    }
    const update = () => setWorkspaceHeight(node.clientHeight);
    update();
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    const wanted = documentKey ? documentKey.split("\n") : [];
    const wantedSet = new Set(wanted);
    const current = pdfsRef.current;
    let removed = false;
    for (const [id, pdf] of [...current.entries()]) {
      if (!wantedSet.has(id)) {
        void pdf.destroy();
        current.delete(id);
        removed = true;
      }
    }
    if (removed) {
      setPdfs(new Map(current));
    }
    let cancelled = false;
    void (async () => {
      for (const id of wanted) {
        if (cancelled || current.has(id)) {
          continue;
        }
        try {
          const pdf = await openDocumentPdf(token, id);
          if (cancelled || !sessionRef.current.documents.some((document) => document.id === id)) {
            void pdf.destroy();
            continue;
          }
          current.set(id, pdf);
          setPdfs(new Map(current));
        } catch (reason: unknown) {
          if (!cancelled) {
            setError(reason instanceof Error ? reason.message : "PDF を表示できませんでした");
          }
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [token, documentKey]);

  useEffect(() => {
    return () => {
      for (const pdf of pdfsRef.current.values()) {
        void pdf.destroy();
      }
      pdfsRef.current.clear();
    };
  }, []);

  function publishSession(updated: SessionState, ticket: number, keepAnchor = false): boolean {
    if (ticket !== ticketRef.current) {
      return false;
    }
    setSession(updated);
    if (!keepAnchor) {
      anchorRef.current = updated.active_page_id;
    }
    return true;
  }

  function showToast(message: string) {
    setToast(message);
    window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(null), 4000);
  }

  async function applySelection(next: PageSelection, keepAnchor = false): Promise<boolean> {
    const ticket = ++ticketRef.current;
    try {
      const updated = await patchSelection(token, next.selected_page_ids, next.active_page_id);
      return publishSession(updated, ticket, keepAnchor);
    } catch (reason: unknown) {
      if (reason instanceof ApiError && (reason.status === 400 || reason.status === 409)) {
        return false;
      }
      setError(reason instanceof Error ? reason.message : "選択を更新できませんでした");
      return false;
    }
  }

  async function runCommand(command: EditCommand) {
    if (busyRef.current) {
      return;
    }
    setRemovedCount(null);
    const ticket = ++ticketRef.current;
    try {
      const result = await postCommand(token, command);
      if (!publishSession(result, ticket)) {
        return;
      }
      if (result.history_dropped) {
        showToast("古い操作履歴を破棄しました");
      }
      if (command.type === "reorder" && !result.changed) {
        showToast("並びは変わっていません");
      }
      if (command.type === "delete" && result.changed) {
        setRemovedCount(command.page_ids.length);
      }
    } catch (reason: unknown) {
      if (ticket !== ticketRef.current) {
        return;
      }
      setError(reason instanceof Error ? reason.message : "編集できませんでした");
    }
  }

  async function saveSettings(next: ViewSettings) {
    if (!settingsReady) {
      return;
    }
    const previous = settingsRef.current;
    setSettings(next);
    try {
      const savedSettings = await putSettings(token, next);
      setSettings(savedSettings);
    } catch (reason: unknown) {
      setSettings(previous);
      setError(reason instanceof Error ? reason.message : "設定を保存できませんでした");
    }
  }

  function stepZoom(direction: 1 | -1) {
    const currentMetrics = metricsRef.current;
    if (!currentMetrics || !sessionRef.current.active_page_id) {
      return;
    }
    const next = changeZoom(zoomRef.current, currentMetrics.effective, direction);
    zoomRef.current = next;
    setZoom(next);
  }

  function move(delta: number) {
    const current = sessionRef.current;
    if (!current.active_page_id) {
      return;
    }
    const next = moveBy(visiblePages(current.pages), current.active_page_id, delta);
    if (next) {
      void applySelection(next);
    }
  }

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (isTextField(event.target) || busyRef.current) {
        return;
      }
      if (event.ctrlKey && !event.altKey && event.key.toLowerCase() === "z") {
        event.preventDefault();
        if (sessionRef.current.undo_enabled) {
          void runCommand({ type: "undo" });
        }
        return;
      }
      if (event.ctrlKey && !event.altKey && event.key.toLowerCase() === "y") {
        event.preventDefault();
        if (sessionRef.current.redo_enabled) {
          void runCommand({ type: "redo" });
        }
        return;
      }
      if (event.key === "Delete") {
        const selected = sessionRef.current.selected_page_ids;
        if (selected.length === 0) {
          return;
        }
        event.preventDefault();
        void runCommand({ type: "delete", page_ids: selected });
        return;
      }
      if (event.key === "Escape") {
        event.preventDefault();
        if (cancelThumbnailDrag()) {
          return;
        }
        void applySelection(clearSelection());
        return;
      }
      const current = sessionRef.current;
      const visible = visiblePages(current.pages);
      if (event.ctrlKey && event.key.toLowerCase() === "a") {
        const thumbs = thumbsRef.current;
        if (!thumbs || !(event.target instanceof Node) || !thumbs.contains(event.target)) {
          return;
        }
        event.preventDefault();
        void applySelection(selectAll(visible, current));
        return;
      }
      const keyDelta: Record<string, number> = {
        ArrowLeft: -1,
        ArrowRight: 1,
        ArrowUp: settingsRef.current.thumbnail_layout === "grid" ? -columnsRef.current : 0,
        ArrowDown: settingsRef.current.thumbnail_layout === "grid" ? columnsRef.current : 0,
      };
      const delta = keyDelta[event.key];
      if (!delta || !current.active_page_id) {
        return;
      }
      event.preventDefault();
      if (event.shiftKey) {
        const anchor = anchorRef.current;
        if (!anchor) {
          return;
        }
        const next = extendBy(visible, anchor, current.active_page_id, delta);
        if (next) {
          void applySelection(next, true);
        }
        return;
      }
      const next = moveBy(visible, current.active_page_id, delta);
      if (next) {
        void applySelection(next);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [token]);

  useEffect(() => {
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey || isTextField(event.target)) {
        return;
      }
      event.preventDefault();
      if (event.deltaY < 0) {
        stepZoom(1);
      } else if (event.deltaY > 0) {
        stepZoom(-1);
      }
    };
    window.addEventListener("wheel", onWheel, { passive: false });
    return () => window.removeEventListener("wheel", onWheel);
  }, [token]);

  async function addFiles(files: File[]) {
    if (loadingRef.current || busyRef.current || files.length === 0) {
      return;
    }
    loadingRef.current = true;
    abortRef.current = false;
    setLoading(true);
    setError(null);
    setSaved(null);
    setProgress(null);
    const received: string[] = [];
    const failures: string[] = [];
    try {
      for (const file of files) {
        if (abortRef.current) {
          break;
        }
        try {
          const result = await postDocument(token, file);
          received.push(result.document_id);
        } catch (reason: unknown) {
          const detail = reason instanceof Error ? reason.message : "読み込めませんでした";
          failures.push(`${file.name}: ${detail}`);
        }
      }
      if (abortRef.current) {
        if (received.length > 0) {
          await discardDocuments(token, received);
        }
        return;
      }
      if (received.length === 0) {
        if (failures.length > 0) {
          setError(failures.join("\n"));
        }
        return;
      }
      const ticket = ++ticketRef.current;
      const next = await postCommand(token, { type: "append_documents", document_ids: received });
      if (!publishSession(next, ticket)) {
        return;
      }
      if (failures.length > 0) {
        setError(failures.join("\n"));
      } else {
        showToast(`${received.length}件の PDF を追加しました`);
      }
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "読み込めませんでした";
      if (!abortRef.current && received.length > 0) {
        try {
          await discardDocuments(token, received);
        } catch (discardReason: unknown) {
          const extra = discardReason instanceof Error ? discardReason.message : "受け取った PDF を破棄できませんでした";
          setError(`${message}\n${extra}`);
          return;
        }
      }
      setError(message);
    } finally {
      loadingRef.current = false;
      abortRef.current = false;
      setLoading(false);
    }
  }

  function onDragOver(event: DragEvent<HTMLDivElement>) {
    if (![...event.dataTransfer.types].includes("Files")) {
      return;
    }
    event.preventDefault();
  }

  function onDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    if (loadingRef.current || busyRef.current) {
      return;
    }
    void addFiles([...event.dataTransfer.files]);
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
      const latest = await getSession(token);
      setSession(latest);
      anchorRef.current = latest.active_page_id;
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

  function selectByPointer(pageId: string, mode: "single" | "toggle" | "range") {
    const current = sessionRef.current;
    const visible = visiblePages(current.pages);
    if (mode === "toggle") {
      void applySelection(ctrlClick(visible, current, pageId));
      return;
    }
    if (mode === "range") {
      void applySelection(shiftClick(visible, current, pageId));
      return;
    }
    void applySelection(singleSelection(pageId));
  }

  async function jump(event: FormEvent) {
    event.preventDefault();
    const next = selectionForPageNumber(pages, jumpText);
    if (!next) {
      setError("ページ番号が範囲外です");
      return;
    }
    const ok = await applySelection(next);
    if (ok) {
      setJumpText("");
    }
  }

  function chooseLayout(layout: ThumbnailLayout) {
    void saveSettings({ ...settings, thumbnail_layout: layout });
  }

  function chooseSize(size: ThumbnailSize) {
    void saveSettings({ ...settings, thumbnail_size: size });
  }

  function chooseZoom(mode: NamedZoom) {
    const next: ZoomState = { kind: "named", mode };
    zoomRef.current = next;
    setZoom(next);
  }

  function onSplitterPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!settingsReady) {
      return;
    }
    event.preventDefault();
    const handle = event.currentTarget;
    handle.setPointerCapture(event.pointerId);
    const startY = event.clientY;
    const startThumb = heights.thumbs;
    const space = available;
    const move = (ev: PointerEvent) => {
      const next = ratioFromThumb(space, startThumb - (ev.clientY - startY));
      if (next != null) {
        setDragRatio(next);
      }
    };
    const up = (ev: PointerEvent) => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      const next = ratioFromThumb(space, startThumb - (ev.clientY - startY));
      setDragRatio(null);
      if (next != null) {
        void saveSettings({ ...settingsRef.current, pane_ratio: next });
      }
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
  }

  const activeIndex = active ? pages.findIndex((page) => page.id === active.id) : -1;
  const previousReason =
    !active ? noPageReason : activeIndex <= 0 ? "先頭のページのため使えません。後ろにページがあるときに使えます。" : undefined;
  const nextReason =
    !active
      ? noPageReason
      : activeIndex < 0 || activeIndex >= pages.length - 1
        ? "末尾のページのため使えません。前にページがあるときに使えます。"
        : undefined;
  const waitingForSize = "表示の寸法を測っています。測り終わると使えます。";
  const zoomOutReason = !active
    ? noPageReason
    : !metrics
      ? waitingForSize
    : zoomUnchanged(zoom, metrics.effective, -1)
      ? zoom.kind === "named" && metrics.effective < 25
        ? "表示が25%未満のため縮小できません。拡大すると25%から段階を変えられます。"
        : "25%より小さくできないため使えません。25%より大きい表示のときに使えます。"
      : undefined;
  const zoomInReason = !active
    ? noPageReason
    : !metrics
      ? waitingForSize
    : zoomUnchanged(zoom, metrics.effective, 1)
      ? "400%より大きくできないため使えません。400%より小さい表示のときに使えます。"
      : undefined;

  const editLocked = busy ? "書き出し中は編集できません。" : undefined;
  const selectionReason = editLocked ?? (session.selected_page_ids.length === 0 ? noPageReason : undefined);
  const emptyMessage =
    pages.length === 0
      ? session.documents.length === 0
        ? "PDF を選ぶと、1 ページ目がここに表示されます。"
        : "見えているページはありません。"
      : active
        ? null
        : selectPrompt;

  function rotateSelection(direction: "left" | "right") {
    const ids = sessionRef.current.selected_page_ids;
    if (ids.length === 0) {
      return;
    }
    void runCommand({ type: "rotate", page_ids: ids, direction });
  }

  function deleteSelection() {
    const ids = sessionRef.current.selected_page_ids;
    if (ids.length === 0) {
      return;
    }
    void runCommand({ type: "delete", page_ids: ids });
  }

  function moveSelection(place: "before" | "after") {
    const current = sessionRef.current;
    if (current.selected_page_ids.length === 0) {
      return;
    }
    const target = visiblePages(current.pages).find((page) => page.order === Number(moveText));
    if (!target) {
      setError("その現在ページ番号はありません");
      return;
    }
    const anchor = place === "before" ? { before_page_id: target.id } : { after_page_id: target.id };
    void runCommand({ type: "reorder", page_ids: current.selected_page_ids, ...anchor });
  }

  function reorderPages(pageIds: string[], anchor: ReorderAnchor) {
    void runCommand({ type: "reorder", page_ids: pageIds, ...anchor });
  }

  return (
    <div className="app" onDragOver={onDragOver} onDrop={onDrop}>
      <header className="toolbar">
        <h1>PDF 編集</h1>
        <label className="file-button">
          PDF を選ぶ
          <input
            type="file"
            accept="application/pdf,.pdf"
            multiple
            disabled={loading || busy}
            onChange={(event) => {
              const files = [...(event.target.files ?? [])];
              event.target.value = "";
              void addFiles(files);
            }}
          />
        </label>
        {loading ? (
          <button type="button" onClick={() => (abortRef.current = true)}>
            読み込みを中止
          </button>
        ) : null}
        <TipButton
          label="元に戻す"
          disabled={Boolean(editLocked) || !session.undo_enabled}
          title={editLocked ?? session.undo_hint}
          onClick={() => void runCommand({ type: "undo" })}
        />
        <TipButton
          label="やり直す"
          disabled={Boolean(editLocked) || !session.redo_enabled}
          title={editLocked ?? session.redo_hint}
          onClick={() => void runCommand({ type: "redo" })}
        />
        <TipButton
          label="左回転"
          disabled={selectionReason !== undefined}
          reason={selectionReason}
          onClick={() => rotateSelection("left")}
        />
        <TipButton
          label="右回転"
          disabled={selectionReason !== undefined}
          reason={selectionReason}
          onClick={() => rotateSelection("right")}
        />
        <TipButton
          label="削除"
          disabled={selectionReason !== undefined}
          reason={selectionReason}
          onClick={deleteSelection}
        />
        <form className="jump" onSubmit={(event) => event.preventDefault()}>
          <input
            aria-label="移動先のページ番号"
            inputMode="numeric"
            value={moveText}
            onChange={(event) => setMoveText(event.target.value)}
          />
          <TipButton
            label="その前へ"
            disabled={selectionReason !== undefined}
            reason={selectionReason}
            onClick={() => moveSelection("before")}
          />
          <TipButton
            label="その後へ"
            disabled={selectionReason !== undefined}
            reason={selectionReason}
            onClick={() => moveSelection("after")}
          />
        </form>
        <TipButton label="前へ" disabled={previousReason !== undefined} reason={previousReason} onClick={() => move(-1)} />
        <TipButton label="次へ" disabled={nextReason !== undefined} reason={nextReason} onClick={() => move(1)} />
        <TipButton
          label="ページ全体"
          pressed={zoom.kind === "named" && zoom.mode === "fit-page"}
          onClick={() => chooseZoom("fit-page")}
        />
        <TipButton
          label="幅に合わせる"
          pressed={zoom.kind === "named" && zoom.mode === "fit-width"}
          onClick={() => chooseZoom("fit-width")}
        />
        <TipButton
          label="100%"
          pressed={zoom.kind === "named" && zoom.mode === "actual"}
          onClick={() => chooseZoom("actual")}
        />
        <TipButton label="−" disabled={zoomOutReason !== undefined} reason={zoomOutReason} onClick={() => stepZoom(-1)} />
        <TipButton label="＋" disabled={zoomInReason !== undefined} reason={zoomInReason} onClick={() => stepZoom(1)} />
        <span className="zoom-label">{metrics && active ? formatPercent(metrics.effective) : ""}</span>
        <TipButton
          label="横一列"
          pressed={settings.thumbnail_layout === "row"}
          disabled={!settingsReady}
          reason="設定の読み込みが終わると使えます。"
          onClick={() => chooseLayout("row")}
        />
        <TipButton
          label="複数列"
          pressed={settings.thumbnail_layout === "grid"}
          disabled={!settingsReady}
          reason="設定の読み込みが終わると使えます。"
          onClick={() => chooseLayout("grid")}
        />
        <TipButton
          label="小"
          pressed={settings.thumbnail_size === "small"}
          disabled={!settingsReady}
          reason="設定の読み込みが終わると使えます。"
          onClick={() => chooseSize("small")}
        />
        <TipButton
          label="中"
          pressed={settings.thumbnail_size === "medium"}
          disabled={!settingsReady}
          reason="設定の読み込みが終わると使えます。"
          onClick={() => chooseSize("medium")}
        />
        <TipButton
          label="大"
          pressed={settings.thumbnail_size === "large"}
          disabled={!settingsReady}
          reason="設定の読み込みが終わると使えます。"
          onClick={() => chooseSize("large")}
        />
        {session.unsaved ? (
          <button
            type="button"
            disabled={pages.length === 0 || busy || loading}
            title={pages.length === 0 ? "見えているページがないため書き出せません。" : undefined}
            onClick={() => void exportAll()}
          >
            未保存
          </button>
        ) : null}
        <button
          type="button"
          className="export-button"
          disabled={pages.length === 0 || busy || loading}
          title={pages.length === 0 ? "見えているページがないため書き出せません。" : undefined}
          onClick={() => void exportAll()}
        >
          全ページを書き出す
        </button>
        <form className="jump" onSubmit={(event) => void jump(event)}>
          <input
            aria-label="ページ番号"
            inputMode="numeric"
            value={jumpText}
            onChange={(event) => setJumpText(event.target.value)}
          />
          <button type="submit">移動</button>
        </form>
        <span className="page-label">
          総ページ数 {pages.length}　選択ページ数 {session.selected_page_ids.length}　元 PDF 数 {session.documents.length}
        </span>
      </header>
      {error ? (
        <p className="notice">
          {error}
          <button type="button" onClick={() => setError(null)}>
            閉じる
          </button>
        </p>
      ) : null}
      {removedCount != null ? (
        <p className="result">
          {removedCount}ページを削除しました
          <button type="button" onClick={() => void runCommand({ type: "undo" })}>
            元に戻す
          </button>
        </p>
      ) : null}
      {toast ? <p className="result">{toast}</p> : null}
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
      <div className="workspace" ref={workspaceRef}>
        <main
          className="stage"
          style={{ height: heights.preview }}
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) {
              void applySelection(clearSelection());
            }
          }}
        >
          {activePdf && active ? (
            <ZoomPreview pdfs={pdfs} pages={pages} activeId={active.id} zoom={zoom} onMetrics={onMetrics} />
          ) : emptyMessage ? (
            <p className="empty">{emptyMessage}</p>
          ) : null}
        </main>
        <div
          className="splitter"
          role="separator"
          aria-orientation="horizontal"
          aria-label="プレビューとサムネイルの境界"
          tabIndex={0}
          onPointerDown={onSplitterPointerDown}
        />
        <section className="thumb-pane" style={{ height: heights.thumbs }} ref={thumbsRef}>
          {pages.length > 0 ? (
            <ThumbnailStrip
              pdfs={pdfs}
              pages={pages}
              names={names}
              selectedIds={session.selected_page_ids}
              activeId={session.active_page_id}
              layout={settings.thumbnail_layout}
              frameHeight={frameHeight}
              onSelect={selectByPointer}
              onRotate={(pageId, direction) => void runCommand({ type: "rotate", page_ids: [pageId], direction })}
              onDelete={(pageId) => void runCommand({ type: "delete", page_ids: [pageId] })}
              onReorder={reorderPages}
              onUnchangedReorder={() => showToast("並びは変わっていません")}
              onBlank={() => void applySelection(clearSelection())}
              onColumns={onColumns}
            />
          ) : null}
        </section>
      </div>
    </div>
  );
}
