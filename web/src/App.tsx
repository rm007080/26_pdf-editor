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
  postPassword,
  postSelectionExport,
  postSessionReset,
  postSplit,
  postSplitCancel,
  postSplitConfirm,
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
import type {
  DocumentIntake,
  EditCommand,
  ExportJob,
  SessionState,
  ThumbnailLayout,
  ThumbnailSize,
  ViewSettings,
} from "./types";
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
  split_positions: [],
  split_unexported: false,
};

const defaultSettings: ViewSettings = {
  thumbnail_layout: "row",
  thumbnail_size: "medium",
  pane_ratio: null,
};

function startFreshConfirmMessage(current: { unsaved: boolean; split_unexported: boolean }): string | null {
  if (current.unsaved && current.split_unexported) {
    return "未保存の変更と、まだ書き出していない分割設定があります。破棄して新しく始めますか？";
  }
  if (current.unsaved) {
    return "未保存の変更があります。破棄して新しく始めますか？";
  }
  if (current.split_unexported) {
    return "まだ書き出していない分割設定があります。破棄して新しく始めますか？";
  }
  return null;
}

const selectPrompt = "ページを選択してください";
const noPageReason = "ページが選択されていないため使えません。ページを選択すると使えます。";
const noVisibleReason = "見えているページがないため書き出せません。";
const noSplitReason = "分割線が無いため使えません。分割線があるときに使えます。";

type SavedResult =
  | { kind: "single"; jobId: string; path: string }
  | { kind: "split"; jobId: string; files: string[] };

type NamingState = {
  names: string[];
  counts: number[];
};

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

const PAGE_LIMIT = 500;
const BYTE_LIMIT = 100 * 1024 * 1024;

type DialogState =
  | { kind: "password"; filename: string; documentId: string; error: string | null }
  | { kind: "confirm"; message: string; confirmLabel: string; enterConfirms?: boolean; warn?: boolean };

type DialogAnswer = DocumentIntake | "confirm" | "cancel" | "abort" | { failed: string };

function isTextField(target: EventTarget | null): boolean {
  return target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement;
}

function largeMessage(filename: string, intake: DocumentIntake): string | null {
  const pagesOver = intake.page_count != null && intake.page_count > PAGE_LIMIT;
  const bytesOver = intake.byte_size > BYTE_LIMIT;
  if (pagesOver && bytesOver && intake.page_count != null) {
    return `${filename} は ${intake.page_count} ページ、${intake.byte_size} バイトあり、500 ページと 100MB（100 × 1024 × 1024 バイト）を超えています。作業領域へ入れますか？`;
  }
  if (pagesOver && intake.page_count != null) {
    return `${filename} は ${intake.page_count} ページあり、500 ページを超えています。作業領域へ入れますか？`;
  }
  if (bytesOver) {
    return `${filename} は ${intake.byte_size} バイトあり、100MB（100 × 1024 × 1024 バイト）を超えています。作業領域へ入れますか？`;
  }
  return null;
}

function EyeIcon({ open }: { open: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      <path
        d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12z"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
      />
      <circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="2" />
      {open ? null : <path d="M4 20L20 4" stroke="currentColor" strokeWidth="2" />}
    </svg>
  );
}

function PasswordDialog({
  filename,
  error,
  onSubmit,
  onCancel,
}: {
  filename: string;
  error: string | null;
  onSubmit: (password: string) => Promise<void>;
  onCancel: () => void;
}) {
  const [password, setPassword] = useState("");
  const [visible, setVisible] = useState(false);
  const [pending, setPending] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    inputRef.current?.focus();
  }, []);
  return (
    <div className="modal-backdrop">
      <form
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="password-title"
        onSubmit={(event) => {
          event.preventDefault();
          if (pending) {
            return;
          }
          setPending(true);
          void onSubmit(password).finally(() => setPending(false));
        }}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            onCancel();
          }
        }}
      >
        <p id="password-title">{filename} を開くパスワードを入力してください。</p>
        {error ? <p className="modal-error">{error}</p> : null}
        <div className="password-row">
          <input
            ref={inputRef}
            aria-label="パスワード"
            type={visible ? "text" : "password"}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
          <button
            type="button"
            className="eye-button"
            aria-label={visible ? "パスワードを隠す" : "パスワードを表示"}
            onClick={() => setVisible((current) => !current)}
          >
            <EyeIcon open={visible} />
          </button>
        </div>
        <div className="modal-actions">
          <button type="submit">開く</button>
          <button type="button" onClick={onCancel}>
            キャンセル
          </button>
        </div>
      </form>
    </div>
  );
}

function ConfirmDialog({
  message,
  confirmLabel,
  enterConfirms = true,
  warn = false,
  onConfirm,
  onCancel,
}: {
  message: string;
  confirmLabel: string;
  enterConfirms?: boolean;
  warn?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const confirmRef = useRef<HTMLButtonElement>(null);
  const formRef = useRef<HTMLFormElement>(null);
  useEffect(() => {
    if (enterConfirms) {
      confirmRef.current?.focus();
      return;
    }
    formRef.current?.focus();
  }, [enterConfirms]);
  return (
    <div className="modal-backdrop">
      <form
        ref={formRef}
        className="modal"
        role="dialog"
        aria-modal="true"
        tabIndex={-1}
        onSubmit={(event) => {
          event.preventDefault();
          if (enterConfirms) {
            onConfirm();
          }
        }}
        onKeyDownCapture={(event) => {
          if (event.key === "Escape") {
            event.preventDefault();
            event.stopPropagation();
            onCancel();
            return;
          }
          if (event.key === "Enter" && !enterConfirms) {
            event.preventDefault();
            event.stopPropagation();
          }
        }}
      >
        <p>{message}</p>
        <div className="modal-actions">
          <button
            type={enterConfirms ? "submit" : "button"}
            className={warn ? "warn-button" : undefined}
            ref={confirmRef}
            onClick={enterConfirms ? undefined : onConfirm}
          >
            {confirmLabel}
          </button>
          <button type="button" onClick={onCancel}>
            キャンセル
          </button>
        </div>
      </form>
    </div>
  );
}

function SplitNamingDialog({
  names,
  counts,
  error,
  busy,
  onChange,
  onConfirm,
  onCancel,
}: {
  names: string[];
  counts: number[];
  error: string | null;
  busy: boolean;
  onChange: (index: number, value: string) => void;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="modal-backdrop">
      <form
        className="modal"
        onSubmit={(event) => {
          event.preventDefault();
          onConfirm();
        }}
      >
        <p>分割した PDF のファイル名を確認してから書き出します。</p>
        {names.map((name, index) => (
          <label className="name-row" key={index}>
            <span>
              {index + 1} 件目（{counts[index]} ページ）
            </span>
            <input
              aria-label={`${index + 1}件目のファイル名`}
              value={name}
              onChange={(event) => onChange(index, event.target.value)}
            />
          </label>
        ))}
        {error ? <p className="modal-error">{error}</p> : null}
        <div className="modal-actions">
          <button type="submit" disabled={busy}>
            書き出す
          </button>
          <button type="button" onClick={onCancel} disabled={busy}>
            取り消し
          </button>
        </div>
      </form>
    </div>
  );
}

function TipButton({
  label,
  disabled,
  reason,
  title,
  pressed,
  warn,
  onClick,
}: {
  label: string;
  disabled?: boolean;
  reason?: string;
  title?: string;
  pressed?: boolean;
  warn?: boolean;
  onClick: () => void;
}) {
  return (
    <span className="tip" title={title ?? (disabled ? reason : undefined)}>
      <button
        type="button"
        className={warn ? "warn-button" : undefined}
        disabled={disabled}
        aria-pressed={pressed}
        onClick={onClick}
      >
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
  const [dialog, setDialog] = useState<DialogState | null>(null);
  const [waiting, setWaiting] = useState<string | null>(null);
  const [progress, setProgress] = useState<ExportJob | null>(null);
  const [saved, setSaved] = useState<SavedResult | null>(null);
  const [naming, setNaming] = useState<NamingState | null>(null);
  const [namingError, setNamingError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resetting, setResetting] = useState(false);
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
  const resettingRef = useRef(false);
  const loadingRef = useRef(false);
  const abortRef = useRef(false);
  const dialogRef = useRef<DialogState | null>(null);
  const namingRef = useRef<NamingState | null>(null);
  const mountedRef = useRef(true);
  const exportMenuRef = useRef<HTMLDetailsElement>(null);
  const splitGate = useRef<{ promise: Promise<void>; resolve: () => void } | null>(null);
  if (splitGate.current === null) {
    let resolve = () => {};
    const promise = new Promise<void>((done) => {
      resolve = done;
    });
    splitGate.current = { promise, resolve };
  }
  const pendingDialog = useRef<((answer: DialogAnswer) => void) | null>(null);
  const pdfsRef = useRef<Map<string, PDFDocumentProxy>>(new Map());
  const toastTimer = useRef(0);
  zoomRef.current = zoom;
  metricsRef.current = metrics;
  sessionRef.current = session;
  settingsRef.current = settings;
  columnsRef.current = columns;
  busyRef.current = busy;
  resettingRef.current = resetting;
  loadingRef.current = loading;
  dialogRef.current = dialog;
  namingRef.current = naming;

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
    mountedRef.current = true;
    let live = true;
    postSplitCancel(token).finally(() => {
      if (live) {
        splitGate.current?.resolve();
      }
    });
    return () => {
      live = false;
      mountedRef.current = false;
    };
  }, [token]);

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
    if (resettingRef.current) {
      return false;
    }
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
    if (busyRef.current || namingRef.current || resettingRef.current) {
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
      if (dialogRef.current || namingRef.current) {
        if (event.key === "Escape") {
          event.preventDefault();
          const current = dialogRef.current;
          if (current?.kind === "confirm" && current.enterConfirms === false) {
            finishDialog("cancel");
          }
        }
        return;
      }
      if (isTextField(event.target) || busyRef.current || resettingRef.current) {
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

  function finishDialog(answer: DialogAnswer) {
    const resolve = pendingDialog.current;
    if (!resolve) {
      return;
    }
    pendingDialog.current = null;
    setDialog(null);
    resolve(answer);
  }

  function requestDialog(next: DialogState): Promise<DialogAnswer> {
    return new Promise((resolve) => {
      pendingDialog.current = resolve;
      setDialog(next);
    });
  }

  function abortLoading() {
    abortRef.current = true;
    finishDialog("abort");
  }

  async function submitPasswordDialog(documentId: string, password: string) {
    try {
      const intake = await postPassword(token, documentId, password);
      if (abortRef.current) {
        return;
      }
      finishDialog(intake);
    } catch (reason: unknown) {
      if (abortRef.current) {
        return;
      }
      if (reason instanceof ApiError && reason.status === 400 && reason.message === "パスワードが違います") {
        setDialog((current) =>
          current && current.kind === "password"
            ? { ...current, error: "パスワードが違います。もう一度入力してください。" }
            : current,
        );
        return;
      }
      const message = reason instanceof Error ? reason.message : "読み込めませんでした";
      finishDialog({ failed: message });
    }
  }

  async function considerFile(file: File): Promise<"cancel" | "abort" | { id: string } | { error: string }> {
    let intake: DocumentIntake;
    try {
      intake = await postDocument(token, file);
    } catch (reason: unknown) {
      const detail = reason instanceof Error ? reason.message : "読み込めませんでした";
      return { error: detail };
    }
    if (abortRef.current) {
      await discardDocuments(token, [intake.document_id]);
      return "abort";
    }
    if (intake.needs_password) {
      const answer = await requestDialog({
        kind: "password",
        filename: file.name,
        documentId: intake.document_id,
        error: null,
      });
      if (answer === "cancel" || answer === "abort") {
        await discardDocuments(token, [intake.document_id]);
        return answer;
      }
      if (typeof answer === "object" && "failed" in answer) {
        await discardDocuments(token, [intake.document_id]);
        return { error: answer.failed };
      }
      if (!(typeof answer === "object" && "document_id" in answer)) {
        await discardDocuments(token, [intake.document_id]);
        return "abort";
      }
      intake = answer;
    }
    if (abortRef.current) {
      await discardDocuments(token, [intake.document_id]);
      return "abort";
    }
    if (intake.signed) {
      const answer = await requestDialog({
        kind: "confirm",
        message: `${file.name} は電子署名 PDF です。編集して書き出すと、署名が有効でなくなる可能性があります。作業領域へ入れますか？`,
        confirmLabel: "入れる",
      });
      if (answer !== "confirm") {
        await discardDocuments(token, [intake.document_id]);
        return answer === "cancel" ? "cancel" : "abort";
      }
    }
    const prompt = largeMessage(file.name, intake);
    if (prompt) {
      const answer = await requestDialog({ kind: "confirm", message: prompt, confirmLabel: "入れる" });
      if (answer !== "confirm") {
        await discardDocuments(token, [intake.document_id]);
        return answer === "cancel" ? "cancel" : "abort";
      }
    }
    if (intake.duplicate) {
      const answer = await requestDialog({
        kind: "confirm",
        message: `${file.name} は、同じ内容の PDF がすでにあります。追加しますか？`,
        confirmLabel: "追加する",
      });
      if (answer !== "confirm") {
        await discardDocuments(token, [intake.document_id]);
        return answer === "cancel" ? "cancel" : "abort";
      }
    }
    return { id: intake.document_id };
  }

  async function addFiles(files: File[]) {
    if (loadingRef.current || busyRef.current || resettingRef.current || files.length === 0) {
      return;
    }
    loadingRef.current = true;
    abortRef.current = false;
    setLoading(true);
    setError(null);
    setSaved(null);
    setProgress(null);
    const accepted: string[] = [];
    const failures: string[] = [];
    try {
      for (const file of files) {
        if (abortRef.current) {
          break;
        }
        const outcome = await considerFile(file);
        if (outcome === "abort") {
          break;
        }
        if (outcome === "cancel") {
          continue;
        }
        if ("error" in outcome) {
          failures.push(`${file.name}: ${outcome.error}`);
          continue;
        }
        accepted.push(outcome.id);
      }
      if (abortRef.current) {
        if (accepted.length > 0) {
          await discardDocuments(token, accepted);
        }
        return;
      }
      if (accepted.length === 0) {
        if (failures.length > 0) {
          setError(failures.join("\n"));
        }
        return;
      }
      const ticket = ++ticketRef.current;
      const next = await postCommand(token, { type: "append_documents", document_ids: accepted });
      if (abortRef.current) {
        // 送信済みの追加は、中止が押された時点では戻せないので Undo で作業領域を戻す。
        const undone = await postCommand(token, { type: "undo" });
        publishSession(undone, ticket);
        return;
      }
      if (!publishSession(next, ticket)) {
        return;
      }
      if (failures.length > 0) {
        setError(failures.join("\n"));
      } else {
        showToast(`${accepted.length}件の PDF を追加しました`);
      }
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "読み込めませんでした";
      if (!abortRef.current && accepted.length > 0) {
        try {
          await discardDocuments(token, accepted);
        } catch (discardReason: unknown) {
          const extra = discardReason instanceof Error ? discardReason.message : "受け取った PDF を破棄できませんでした";
          setError(`${message}\n${extra}`);
          return;
        }
      }
      setError(message);
    } finally {
      finishDialog("abort");
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
    if (loadingRef.current || busyRef.current || resettingRef.current || dialogRef.current) {
      return;
    }
    void addFiles([...event.dataTransfer.files]);
  }

  async function openResult(kind: "open-file" | "open-folder", index?: number) {
    if (!saved) {
      return;
    }
    try {
      await openSaved(token, saved.jobId, kind, index);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "開けませんでした");
    }
  }

  async function watchExport(jobId: string, kind: "single" | "split") {
    let job = await getExportJob(token, jobId);
    setProgress(job);
    setWaiting(null);
    while (job.stage !== "completed" && job.stage !== "failed") {
      await delay(300);
      job = await getExportJob(token, jobId);
      setProgress(job);
    }
    if (job.stage === "failed") {
      setError(job.detail ?? "書き出しに失敗しました");
      return;
    }
    const latest = await getSession(token);
    setSession(latest);
    anchorRef.current = latest.active_page_id;
    if (kind === "split") {
      setSaved({ kind: "split", jobId, files: job.files ?? [] });
      return;
    }
    if (job.path) {
      setSaved({ kind: "single", jobId, path: job.path });
    }
  }

  async function exportAll() {
    if (namingRef.current) {
      return;
    }
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
      await watchExport(started.job_id, "single");
    } catch (reason: unknown) {
      setWaiting(null);
      setError(reason instanceof Error ? reason.message : "書き出せませんでした");
    } finally {
      setBusy(false);
    }
  }

  async function exportSelection() {
    if (namingRef.current) {
      return;
    }
    closeExportMenu();
    setBusy(true);
    setError(null);
    setSaved(null);
    setProgress(null);
    setWaiting("保存先を選択しています");
    try {
      const started = await postSelectionExport(token);
      if ("status" in started) {
        setWaiting(null);
        return;
      }
      await watchExport(started.job_id, "single");
    } catch (reason: unknown) {
      setWaiting(null);
      setError(reason instanceof Error ? reason.message : "書き出せませんでした");
    } finally {
      setBusy(false);
    }
  }

  function closeExportMenu() {
    if (exportMenuRef.current) {
      exportMenuRef.current.open = false;
    }
  }

  async function exportSplit() {
    if (namingRef.current) {
      return;
    }
    closeExportMenu();
    await splitGate.current?.promise;
    if (!mountedRef.current || namingRef.current) {
      return;
    }
    setBusy(true);
    setError(null);
    setSaved(null);
    setProgress(null);
    setWaiting("保存先フォルダを選択しています");
    try {
      const started = await postSplit(token);
      if (!mountedRef.current) {
        await postSplitCancel(token);
        return;
      }
      if (started.status === "cancelled") {
        setWaiting(null);
        return;
      }
      setNaming({
        names: started.files.map((file) => file.name),
        counts: started.files.map((file) => file.pages),
      });
      setNamingError(null);
      setWaiting(null);
    } catch (reason: unknown) {
      setWaiting(null);
      if (mountedRef.current) {
        setError(reason instanceof Error ? reason.message : "書き出せませんでした");
      } else {
        await postSplitCancel(token);
      }
    } finally {
      setBusy(false);
    }
  }

  async function confirmSplitNames() {
    if (!naming) {
      return;
    }
    setBusy(true);
    setNamingError(null);
    setError(null);
    setSaved(null);
    setProgress(null);
    try {
      const started = await postSplitConfirm(token, naming.names);
      setNaming(null);
      await watchExport(started.job_id, "split");
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 400) {
        setNamingError(reason.message);
        return;
      }
      setError(reason instanceof Error ? reason.message : "書き出せませんでした");
    } finally {
      setBusy(false);
    }
  }

  async function cancelSplitNames() {
    setBusy(true);
    try {
      await postSplitCancel(token);
      setNaming(null);
      setNamingError(null);
      setWaiting(null);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "取り消しできませんでした");
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

  const editLocked = resetting
    ? "新しく始めているため編集できません。"
    : busy
      ? "書き出し中は編集できません。"
      : naming
        ? "ファイル名の入力中は編集できません。"
        : undefined;
  const startFreshReason = loading
    ? "読み込み中は新しく始められません。読み込みが終わると使えます。"
    : dialog
      ? "確認が終わるまで新しく始められません。"
      : naming
        ? "ファイル名の確定中は新しく始められません。確定が終わると使えます。"
        : busy
          ? waiting
            ? "保存先の選択中は新しく始められません。選択が終わると使えます。"
            : "書き出し中は新しく始められません。書き出しが終わると使えます。"
          : resetting
            ? "新しく始めているため使えません。"
            : undefined;
  const exportLocked = pages.length === 0 || busy || loading || naming !== null;
  const exportReason = pages.length === 0 ? noVisibleReason : editLocked;
  const selectionExportReason =
    exportReason ?? (session.selected_page_ids.length === 0 ? noPageReason : undefined);
  const clearSplitsReason = editLocked ?? (session.split_positions.length === 0 ? noSplitReason : undefined);
  const selectionReason = editLocked ?? (session.selected_page_ids.length === 0 ? noPageReason : undefined);
  const emptyMessage =
    pages.length === 0
      ? session.documents.length === 0
        ? "PDF を選ぶと、1 ページ目がここに表示されます。"
        : "見えているページはありません。"
      : active
        ? null
        : selectPrompt;

  async function startFresh() {
    if (resettingRef.current || loadingRef.current || busyRef.current || dialogRef.current || namingRef.current) {
      return;
    }
    const message = startFreshConfirmMessage(sessionRef.current);
    if (message) {
      const answer = await requestDialog({
        kind: "confirm",
        message,
        confirmLabel: "破棄して新しく始める",
        enterConfirms: false,
        warn: true,
      });
      if (answer !== "confirm") {
        return;
      }
      if (loadingRef.current || busyRef.current || namingRef.current || resettingRef.current) {
        return;
      }
    }
    resettingRef.current = true;
    setResetting(true);
    try {
      const next = await postSessionReset(token);
      const ticket = ++ticketRef.current;
      publishSession(next, ticket);
      setSaved(null);
      setProgress(null);
      setWaiting(null);
      setRemovedCount(null);
      setError(null);
      setToast(null);
      window.clearTimeout(toastTimer.current);
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 409) {
        setError(reason.message);
        return;
      }
      setError(reason instanceof Error ? reason.message : "新しく始められませんでした");
    } finally {
      resettingRef.current = false;
      setResetting(false);
    }
  }

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
            disabled={loading || busy || dialog !== null}
            onChange={(event) => {
              const files = [...(event.target.files ?? [])];
              event.target.value = "";
              void addFiles(files);
            }}
          />
        </label>
        {loading ? (
          <button type="button" onClick={abortLoading}>
            読み込みを中止
          </button>
        ) : null}
        <TipButton
          label="新しく始める"
          warn
          disabled={startFreshReason !== undefined}
          reason={startFreshReason}
          onClick={() => void startFresh()}
        />
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
        <TipButton
          label="すべての分割線を解除"
          disabled={clearSplitsReason !== undefined}
          reason={clearSplitsReason}
          onClick={() => void runCommand({ type: "clear_all_splits" })}
        />
        {session.unsaved ? (
          <button type="button" disabled={exportLocked} title={exportReason} onClick={() => void exportAll()}>
            未保存
          </button>
        ) : null}
        {session.split_unexported ? (
          <button type="button" disabled={exportLocked} title={exportReason} onClick={() => void exportSplit()}>
            分割設定 未出力
          </button>
        ) : null}
        <button
          type="button"
          className="export-button"
          disabled={exportLocked}
          title={exportReason}
          onClick={() => void exportAll()}
        >
          全ページを書き出す
        </button>
        <details className="export-menu" ref={exportMenuRef}>
          <summary
            onClick={(event) => {
              if (exportLocked) {
                event.preventDefault();
              }
            }}
          >
            分割・選択ページ
          </summary>
          <div className="menu-panel">
            <TipButton
              label="分割"
              disabled={exportLocked}
              reason={exportReason}
              onClick={() => void exportSplit()}
            />
            <TipButton
              label="選択ページ"
              disabled={selectionExportReason !== undefined}
              reason={selectionExportReason}
              onClick={() => void exportSelection()}
            />
          </div>
        </details>
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
      {saved?.kind === "single" ? (
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
      {saved?.kind === "split" ? (
        <div className="result">
          {saved.files.map((path, index) => (
            <span key={`${path}-${index}`}>
              ファイル名 {fileNameOf(path)}
              <button type="button" onClick={() => void openResult("open-file", index)}>
                PDF を開く
              </button>
            </span>
          ))}
          {saved.files[0] ? <span>保存先 {directoryOf(saved.files[0])}</span> : null}
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
              splitPositions={session.split_positions}
              splitDisabled={editLocked !== undefined}
              splitReason={editLocked}
              onToggleSplit={(pageId, present) =>
                void runCommand(
                  present
                    ? { type: "clear_split", after_page_id: pageId }
                    : { type: "set_split", after_page_id: pageId },
                )
              }
            />
          ) : null}
        </section>
      </div>
      {dialog?.kind === "password" ? (
        <PasswordDialog
          key={dialog.documentId}
          filename={dialog.filename}
          error={dialog.error}
          onSubmit={(password) => submitPasswordDialog(dialog.documentId, password)}
          onCancel={() => finishDialog("cancel")}
        />
      ) : null}
      {naming ? (
        <SplitNamingDialog
          names={naming.names}
          counts={naming.counts}
          error={namingError}
          busy={busy}
          onChange={(index, value) => {
            setNaming((current) => {
              if (!current) {
                return current;
              }
              const names = current.names.slice();
              names[index] = value;
              return { ...current, names };
            });
          }}
          onConfirm={() => void confirmSplitNames()}
          onCancel={() => void cancelSplitNames()}
        />
      ) : null}
      {dialog?.kind === "confirm" ? (
        <ConfirmDialog
          message={dialog.message}
          confirmLabel={dialog.confirmLabel}
          enterConfirms={dialog.enterConfirms}
          warn={dialog.warn}
          onConfirm={() => finishDialog("confirm")}
          onCancel={() => finishDialog("cancel")}
        />
      ) : null}
    </div>
  );
}
