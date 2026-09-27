import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type FormEvent, type PointerEvent as ReactPointerEvent } from "react";
import type { PDFDocumentProxy } from "pdfjs-dist";
import {
  ApiError,
  discardDocuments,
  getAbout,
  getExportJob,
  getSession,
  getSettings,
  openSaved,
  patchSelection,
  postActivity,
  postClaim,
  postCommand,
  postDocument,
  postDuplicate,
  postExport,
  postHeartbeat,
  postLogsOpen,
  postOpenPdfs,
  postPassword,
  postSelectionExport,
  postSessionReset,
  postSettingsReset,
  postShutdown,
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
  IdleMinutes,
  OpenedDocument,
  RejectedDocument,
  SessionState,
  ThemeChoice,
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
  theme: null,
  windows_notifications: true,
  idle_minutes: 120,
  last_open_dir: null,
  last_save_dir: null,
};

type AppView = "edit" | "settings" | "help";

const TOOLBAR_COLLAPSE_ORDER = [
  "exit",
  "start-fresh",
  "layout",
  "clear-splits",
  "redo",
  "undo",
  "rotate-left",
  "rotate-right",
  "delete",
] as const;

const IDLE_CHOICES: { value: IdleMinutes; label: string }[] = [
  { value: 30, label: "30分" },
  { value: 60, label: "1時間" },
  { value: 120, label: "2時間" },
  { value: 240, label: "4時間" },
];

function viewFromPath(pathname: string): AppView {
  if (pathname === "/settings") {
    return "settings";
  }
  if (pathname === "/help") {
    return "help";
  }
  return "edit";
}

function isOpened(item: OpenedDocument | RejectedDocument): item is OpenedDocument {
  return "document_id" in item;
}

const SECOND_TAB_MESSAGE =
  "編集画面は別のタブで開いています。このタブでは編集できません。開いているタブに戻ってください。";
const UNSUPPORTED_BROWSER =
  "このブラウザは正式な対応対象ではありません。Microsoft Edge または Google Chrome を推奨します。このまま使うこともできます。";
const LEFTOVER_MESSAGE = "前回の終了が完了しなかったため、一時データを消去しました。";
const OFFLINE_MESSAGE = "バックエンドに届かないため、編集状態は表示しません。";

type Access = "pending" | "editing" | "blocked" | "offline";

function unsupportedBrowser(ua: string): string | null {
  const edge = ua.includes("Edg/");
  const chrome = ua.includes("Chrome/") && !ua.includes("Edg/") && !ua.includes("OPR/");
  if (edge || chrome) {
    return null;
  }
  return UNSUPPORTED_BROWSER;
}

function idleLossText(current: { unsaved: boolean; split_unexported: boolean }): string {
  if (current.unsaved && current.split_unexported) {
    return "未保存の変更と、まだ書き出していない分割設定があります。";
  }
  if (current.unsaved) {
    return "未保存の変更があります。";
  }
  if (current.split_unexported) {
    return "まだ書き出していない分割設定があります。";
  }
  return "";
}

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

function Icon({ name }: { name: string }) {
  const common = { fill: "none", stroke: "currentColor", strokeWidth: 2, strokeLinecap: "round" as const, strokeLinejoin: "round" as const };
  return (
    <svg className="tool-icon" viewBox="0 0 24 24" width="20" height="20" aria-hidden="true">
      {name === "start" ? <path {...common} d="M4 6h16M4 12h10M4 18h16" /> : null}
      {name === "undo" ? <path {...common} d="M8 8H4v4M4 8c3 6 8 8 16 6" /> : null}
      {name === "redo" ? <path {...common} d="M16 8h4v4M20 8c-3 6-8 8-16 6" /> : null}
      {name === "left" ? <path {...common} d="M8 8l-4 4 4 4M4 12h10a6 6 0 0 1 0 0" /> : null}
      {name === "right" ? <path {...common} d="M16 8l4 4-4 4M20 12H10" /> : null}
      {name === "trash" ? <path {...common} d="M5 7h14M9 7V5h6v2M8 7l1 12h6l1-12" /> : null}
      {name === "clear" ? <path {...common} d="M6 6l12 12M8 4h8M6 20h12" /> : null}
      {name === "row" ? <path {...common} d="M4 8h4v8H4zM10 8h4v8h-4zM16 8h4v8h-4z" /> : null}
      {name === "grid" ? <path {...common} d="M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z" /> : null}
      {name === "export" ? <path {...common} d="M12 4v10M8 10l4 4 4-4M5 20h14" /> : null}
      {name === "exit" ? <path {...common} d="M10 7V5H5v14h5v-2M10 12h10M16 8l4 4-4 4" /> : null}
    </svg>
  );
}

function TipButton({
  label,
  icon,
  disabled,
  reason,
  title,
  pressed,
  warn,
  onClick,
}: {
  label: string;
  icon?: string;
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
        className={warn ? "tool warn-button" : "tool"}
        disabled={disabled}
        aria-pressed={pressed}
        onClick={onClick}
      >
        {icon ? <Icon name={icon} /> : null}
        <span className="tool-label">{label}</span>
      </button>
    </span>
  );
}

function SettingsScreen({
  settings,
  ready,
  about,
  updateNote,
  onTheme,
  onNotifications,
  onIdle,
  onReset,
  onOpenLogs,
  onCheckUpdates,
}: {
  settings: ViewSettings;
  ready: boolean;
  about: { name: string; version: string } | null;
  updateNote: string | null;
  onTheme: (theme: ThemeChoice) => void;
  onNotifications: (enabled: boolean) => void;
  onIdle: (minutes: IdleMinutes) => void;
  onReset: () => void;
  onOpenLogs: () => void;
  onCheckUpdates: () => void;
}) {
  return (
    <section className="page">
      <h2>設定</h2>
      <p className="hint">選んでいないあいだは、この端末の色に合わせます。選んだあとは、その選択を記憶します。</p>
      <div className="choice-row">
        <TipButton label="ライト" pressed={settings.theme === "light"} disabled={!ready} onClick={() => onTheme("light")} />
        <TipButton label="ダーク" pressed={settings.theme === "dark"} disabled={!ready} onClick={() => onTheme("dark")} />
      </div>
      <label className="choice-row">
        <input
          type="checkbox"
          checked={settings.windows_notifications}
          disabled={!ready}
          onChange={(event) => onNotifications(event.target.checked)}
        />
        Windows 通知
      </label>
      <label className="choice-row">
        自動終了までの時間
        <select
          aria-label="自動終了までの時間"
          value={settings.idle_minutes}
          disabled={!ready}
          onChange={(event) => onIdle(Number(event.target.value) as IdleMinutes)}
        >
          {IDLE_CHOICES.map((choice) => (
            <option key={choice.value} value={choice.value}>
              {choice.label}
            </option>
          ))}
        </select>
      </label>
      <p className="hint">この画面では終了のタイマーは動きません。選んだ時間だけを保存します。</p>
      <button type="button" className="tool warn-button" disabled={!ready} onClick={onReset}>
        <span className="tool-label">設定を初期状態に戻す</span>
      </button>
      <div className="about-block">
        <p>{about ? `${about.name} ${about.version}` : "PDF編集"}</p>
        <button type="button" className="tool" onClick={onCheckUpdates}>
          <span className="tool-label">更新を確認</span>
        </button>
        {updateNote ? <p className="hint">{updateNote}</p> : null}
        <button type="button" className="tool" onClick={onOpenLogs}>
          <span className="tool-label">エラーログの保存先を開く</span>
        </button>
      </div>
    </section>
  );
}

function HelpScreen() {
  return (
    <section className="page">
      <h2>ヘルプ</h2>
      <h3>基本操作</h3>
      <p>PDF を選ぶか、画面へドロップして読み込みます。複数のときは選んだ順、またはドロップした順で、1 つの作業領域へページが並びます。サムネイルをドラッグして並べ替え、回転と削除は、そのページか選択全体に使います。書き出しは、全ページ、選択ページ、分割の 3 種類です。</p>
      <h3>複数選択</h3>
      <p>クリックで 1 ページを選びます。Ctrl+クリックで加えたり外したりします。Shift+クリックは、その範囲だけを選びます。Ctrl+A は、サムネイル一覧にフォーカスがあるとき全ページです。余白のクリック、または Esc で選択を解除します。</p>
      <h3>分割</h3>
      <p>隣り合うサムネイルのあいだをクリックすると分割線が入り、同じ場所をもう一度クリックすると外れます。複数列でも、現在ページ番号で隣り合う境目です。「すべての分割線を解除」は、それとは別の 1 操作です。分割の書き出しは、その線で分かれた PDF になります。</p>
      <figure className="help-figure">
        <span className="help-page">1</span>
        <span className="help-page">2</span>
        <span className="help-split" />
        <span className="help-page">3</span>
        <span className="help-page">4</span>
      </figure>
      <h3>ショートカット</h3>
      <p>Delete で選択したページを削除します。Ctrl+Z で元に戻し、Ctrl+Y でやり直します。矢印キーでページを移動し、Shift+矢印で範囲を広げたり縮めたりします。Ctrl+ホイールで拡大縮小します。入力欄にフォーカスがあるときは、その欄の操作が優先されます。</p>
    </section>
  );
}

export function App() {
  const token = useMemo(() => readToken(), []);
  const [access, setAccess] = useState<Access>("pending");
  const [browserNotice, setBrowserNotice] = useState<string | null>(() => unsupportedBrowser(navigator.userAgent));
  const [leftoverNotice, setLeftoverNotice] = useState(false);
  const [idleWarning, setIdleWarning] = useState(false);
  const [warningDismissed, setWarningDismissed] = useState(false);
  const [session, setSession] = useState<SessionState>(emptySession);
  const [settings, setSettings] = useState<ViewSettings>(defaultSettings);
  const [settingsReady, setSettingsReady] = useState(false);
  const [view, setView] = useState<AppView>(() => viewFromPath(window.location.pathname));
  const [picking, setPicking] = useState(false);
  const [held, setHeld] = useState<string[]>([]);
  const [about, setAbout] = useState<{ name: string; version: string; cleared_leftover: boolean } | null>(null);
  const [updateNote, setUpdateNote] = useState<string | null>(null);
  const [iconsOnly, setIconsOnly] = useState(false);
  const [overflow, setOverflow] = useState<string[]>([]);
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
  const pickingRef = useRef(false);
  const toolbarRef = useRef<HTMLElement>(null);
  const abortRef = useRef(false);
  const dialogRef = useRef<DialogState | null>(null);
  const namingRef = useRef<NamingState | null>(null);
  const mountedRef = useRef(true);
  const accessRef = useRef<Access>("pending");
  const quittingRef = useRef(false);
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
  pickingRef.current = picking;
  dialogRef.current = dialog;
  namingRef.current = naming;
  accessRef.current = access;

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
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (access !== "editing") {
      return;
    }
    let live = true;
    postSplitCancel(token).finally(() => {
      if (live) {
        splitGate.current?.resolve();
      }
    });
    return () => {
      live = false;
    };
  }, [token, access]);

  useEffect(() => {
    let cancelled = false;
    let claiming = false;
    let heartbeatTimer = 0;

    function noteWarning(warning: boolean) {
      if (!warning) {
        setIdleWarning(false);
        setWarningDismissed(false);
        return;
      }
      setIdleWarning(true);
    }

    async function loadEditor() {
      const [next, savedSettings, info] = await Promise.all([getSession(token), getSettings(token), getAbout(token)]);
      if (cancelled) {
        return;
      }
      setSession(next);
      anchorRef.current = next.active_page_id;
      setSettings(savedSettings);
      setSettingsReady(true);
      setAbout(info);
      setLeftoverNotice(info.cleared_leftover);
      accessRef.current = "editing";
      setAccess("editing");
      setError(null);
    }

    async function beat() {
      if (quittingRef.current || accessRef.current !== "editing") {
        return;
      }
      try {
        const body = await postHeartbeat(token);
        if (!cancelled) {
          noteWarning(body.idle_warning);
        }
      } catch (reason: unknown) {
        if (cancelled || accessRef.current !== "editing") {
          return;
        }
        try {
          await postClaim(token);
          const next = await getSession(token);
          if (cancelled) {
            return;
          }
          setSession(next);
          anchorRef.current = next.active_page_id;
          setError(null);
        } catch (claimError: unknown) {
          if (cancelled) {
            return;
          }
          window.clearInterval(heartbeatTimer);
          if (claimError instanceof ApiError && claimError.status === 409) {
            accessRef.current = "blocked";
            setAccess("blocked");
            return;
          }
          accessRef.current = "offline";
          setAccess("offline");
          setError(OFFLINE_MESSAGE);
        }
      }
    }

    async function tryClaim() {
      if (cancelled || claiming || quittingRef.current || accessRef.current === "editing") {
        return;
      }
      claiming = true;
      try {
        await postClaim(token);
        if (cancelled) {
          return;
        }
        await loadEditor();
        if (cancelled) {
          return;
        }
        window.clearInterval(heartbeatTimer);
        void beat();
        heartbeatTimer = window.setInterval(() => void beat(), 2000);
      } catch (reason: unknown) {
        if (cancelled) {
          return;
        }
        if (reason instanceof ApiError && reason.status === 409) {
          accessRef.current = "blocked";
          setAccess("blocked");
          return;
        }
        accessRef.current = "offline";
        setAccess("offline");
        setError(reason instanceof Error ? reason.message : OFFLINE_MESSAGE);
      } finally {
        claiming = false;
      }
    }

    const onVisible = () => {
      if (document.visibilityState === "visible") {
        void beat();
      }
    };
    // pagehide と F5 では release を送らない。sendBeacon も使わない。
    document.addEventListener("visibilitychange", onVisible);
    void tryClaim();
    const claimTimer = window.setInterval(() => void tryClaim(), 2000);
    return () => {
      cancelled = true;
      window.clearInterval(claimTimer);
      window.clearInterval(heartbeatTimer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [token]);

  useEffect(() => {
    if (access !== "editing") {
      return;
    }
    let lastSent = 0;
    const send = (source: "click" | "key" | "scroll") => {
      const now = Date.now();
      if (now - lastSent < 1000) {
        return;
      }
      lastSent = now;
      void postActivity(token, source).catch(() => undefined);
    };
    const onClick = (event: MouseEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest("[data-idle-dismiss]")) {
        return;
      }
      send("click");
    };
    const onKey = (event: KeyboardEvent) => {
      const target = event.target;
      if (target instanceof Element && target.closest("[data-idle-dismiss]")) {
        return;
      }
      send("key");
    };
    const onScroll = () => send("scroll");
    window.addEventListener("click", onClick);
    window.addEventListener("keydown", onKey);
    window.addEventListener("scroll", onScroll, true);
    return () => {
      window.removeEventListener("click", onClick);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", onScroll, true);
    };
  }, [token, access]);

  useEffect(() => {
    const onPop = () => setView(viewFromPath(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    const root = document.documentElement;
    if (settings.theme === "light" || settings.theme === "dark") {
      root.dataset.theme = settings.theme;
    } else {
      delete root.dataset.theme;
    }
  }, [settings.theme]);

  useEffect(() => {
    if (view !== "settings" || access !== "editing") {
      return;
    }
    let live = true;
    getAbout(token)
      .then((next) => {
        if (live) {
          setAbout(next);
        }
      })
      .catch((reason: unknown) => {
        if (live) {
          setError(reason instanceof Error ? reason.message : "バージョンを読めませんでした");
        }
      });
    return () => {
      live = false;
    };
  }, [token, view, access]);

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
  }, [access]);

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
      try {
        await postActivity(token, "settings");
      } catch {
        // 設定は保存済み。計測のやり直しだけ失敗しても、表示は戻さない。
      }
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

  function holdNotice(message: string) {
    setHeld((current) => (current.includes(message) ? current : [...current, message]));
  }

  function openView(next: AppView) {
    const path = next === "edit" ? "/" : `/${next}`;
    if (window.location.pathname !== path) {
      window.history.pushState(null, "", path);
    }
    setView(next);
  }

  async function reviewIntake(
    intake: DocumentIntake,
    filename: string,
    approved: string[],
  ): Promise<"cancel" | "abort" | { id: string } | { error: string }> {
    if (abortRef.current) {
      await discardDocuments(token, [intake.document_id]);
      return "abort";
    }
    if (intake.needs_password) {
      const answer = await requestDialog({
        kind: "password",
        filename,
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
    let duplicate = false;
    try {
      duplicate = (await postDuplicate(token, intake.document_id, approved)).duplicate;
    } catch (reason: unknown) {
      await discardDocuments(token, [intake.document_id]);
      return { error: reason instanceof Error ? reason.message : "重複を確認できませんでした" };
    }
    if (intake.signed) {
      const answer = await requestDialog({
        kind: "confirm",
        message: `${filename} は電子署名 PDF です。編集して書き出すと、署名が有効でなくなる可能性があります。作業領域へ入れますか？`,
        confirmLabel: "入れる",
      });
      if (answer !== "confirm") {
        await discardDocuments(token, [intake.document_id]);
        return answer === "cancel" ? "cancel" : "abort";
      }
    }
    const prompt = largeMessage(filename, intake);
    if (prompt) {
      const answer = await requestDialog({ kind: "confirm", message: prompt, confirmLabel: "入れる" });
      if (answer !== "confirm") {
        await discardDocuments(token, [intake.document_id]);
        return answer === "cancel" ? "cancel" : "abort";
      }
    }
    if (duplicate) {
      const answer = await requestDialog({
        kind: "confirm",
        message: `${filename} は、同じ内容の PDF がすでにあります。追加しますか？`,
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
    if (loadingRef.current || busyRef.current || resettingRef.current || pickingRef.current || files.length === 0) {
      return;
    }
    loadingRef.current = true;
    abortRef.current = false;
    setLoading(true);
    setError(null);
    setSaved(null);
    setProgress(null);
    const accepted: string[] = [];
    const approved: string[] = [];
    const failures: string[] = [];
    try {
      for (const file of files) {
        if (abortRef.current) {
          break;
        }
        let intake: DocumentIntake;
        try {
          intake = await postDocument(token, file);
        } catch (reason: unknown) {
          failures.push(`${file.name}: ${reason instanceof Error ? reason.message : "読み込めませんでした"}`);
          continue;
        }
        const outcome = await reviewIntake(intake, file.name, approved);
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
        approved.push(outcome.id);
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

  async function pickPdfs() {
    if (loadingRef.current || busyRef.current || resettingRef.current || dialogRef.current || pickingRef.current) {
      return;
    }
    pickingRef.current = true;
    setPicking(true);
    setWaiting("ファイルを選択しています");
    loadingRef.current = true;
    abortRef.current = false;
    setLoading(true);
    const accepted: string[] = [];
    const approved: string[] = [];
    const failures: string[] = [];
    try {
      const opened = await postOpenPdfs(token);
      if (opened.initial_folder_notice) {
        holdNotice(opened.initial_folder_notice);
      }
      if (opened.status === "cancelled") {
        return;
      }
      for (const item of opened.items ?? []) {
        if (abortRef.current) {
          break;
        }
        if (!isOpened(item)) {
          failures.push(`${item.filename}: ${item.detail}`);
          continue;
        }
        const outcome = await reviewIntake(item, item.filename, approved);
        if (outcome === "abort") {
          break;
        }
        if (outcome === "cancel") {
          continue;
        }
        if ("error" in outcome) {
          failures.push(`${item.filename}: ${outcome.error}`);
          continue;
        }
        accepted.push(outcome.id);
        approved.push(outcome.id);
      }
      if (failures.length > 0) {
        for (const line of failures) {
          holdNotice(line);
        }
      }
      if (abortRef.current) {
        if (accepted.length > 0) {
          await discardDocuments(token, accepted);
        }
        return;
      }
      if (accepted.length === 0) {
        return;
      }
      const ticket = ++ticketRef.current;
      const next = await postCommand(token, { type: "append_documents", document_ids: accepted });
      if (!publishSession(next, ticket)) {
        return;
      }
      if (failures.length === 0) {
        showToast(`${accepted.length}件の PDF を追加しました`);
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "ファイル選択を完了できませんでした");
    } finally {
      finishDialog("abort");
      loadingRef.current = false;
      abortRef.current = false;
      pickingRef.current = false;
      setLoading(false);
      setPicking(false);
      setWaiting(null);
    }
  }

  async function resetSettings() {
    const answer = await requestDialog({
      kind: "confirm",
      message: "設定を初期状態に戻しますか？",
      confirmLabel: "初期状態に戻す",
      enterConfirms: false,
    });
    if (answer !== "confirm") {
      return;
    }
    try {
      const next = await postSettingsReset(token);
      setSettings(next);
      try {
        await postActivity(token, "settings");
      } catch {
        // 設定は初期化済み。計測のやり直しだけ失敗しても、表示は戻さない。
      }
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "設定を初期状態に戻せませんでした");
    }
  }

  async function openLogs() {
    try {
      await postLogsOpen(token);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "ログの保存先を開けませんでした");
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
    if (loadingRef.current || busyRef.current || resettingRef.current || dialogRef.current || pickingRef.current) {
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
      if (started.initial_folder_notice) {
        holdNotice(started.initial_folder_notice);
      }
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
      if (started.initial_folder_notice) {
        holdNotice(started.initial_folder_notice);
      }
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
      if (started.initial_folder_notice) {
        holdNotice(started.initial_folder_notice);
      }
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
    : picking
      ? "ファイルの選択中は編集できません。"
      : busy
        ? "書き出し中は編集できません。"
        : naming
          ? "ファイル名の入力中は編集できません。"
          : undefined;
  const startFreshReason = loading || picking
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
  const exportLocked = pages.length === 0 || busy || loading || picking || naming !== null;
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

  async function continueWork() {
    try {
      await postActivity(token, "continue");
      setIdleWarning(false);
      setWarningDismissed(false);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "計測をやり直せませんでした");
    }
  }

  async function quit() {
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
    }
    try {
      quittingRef.current = true;
      await postShutdown(token, true);
      accessRef.current = "offline";
      setAccess("offline");
      setError(null);
    } catch (reason: unknown) {
      quittingRef.current = false;
      setError(reason instanceof Error ? reason.message : "終了できませんでした");
    }
  }

  async function startFresh() {
    if (
      resettingRef.current ||
      loadingRef.current ||
      busyRef.current ||
      dialogRef.current ||
      namingRef.current ||
      pickingRef.current
    ) {
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
      if (loadingRef.current || busyRef.current || namingRef.current || resettingRef.current || pickingRef.current) {
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

  useLayoutEffect(() => {
    const bar = toolbarRef.current;
    if (!bar || view !== "edit") {
      return;
    }
    const measure = () => {
      const nodes = [...bar.querySelectorAll<HTMLElement>("[data-collapse]")];
      const menu = bar.querySelector<HTMLElement>("[data-overflow-menu]");
      bar.classList.remove("icons-only");
      for (const node of nodes) {
        node.hidden = false;
      }
      if (menu) {
        menu.hidden = true;
      }
      let hideLabels = false;
      const hidden: string[] = [];
      if (bar.scrollWidth > bar.clientWidth + 1) {
        hideLabels = true;
        bar.classList.add("icons-only");
        if (bar.scrollWidth > bar.clientWidth + 1) {
          if (menu) {
            menu.hidden = false;
          }
          for (const id of TOOLBAR_COLLAPSE_ORDER) {
            const node = bar.querySelector<HTMLElement>(`[data-collapse="${id}"]`);
            if (!node) {
              continue;
            }
            node.hidden = true;
            hidden.push(id);
            if (bar.scrollWidth <= bar.clientWidth + 1) {
              break;
            }
          }
        }
      }
      setIconsOnly((current) => (current === hideLabels ? current : hideLabels));
      setOverflow((current) => (current.join() === hidden.join() ? current : hidden));
    };
    measure();
    const observer = new ResizeObserver(() => measure());
    observer.observe(bar);
    return () => observer.disconnect();
  }, [view, loading, session.unsaved, session.split_unexported, settingsReady, access]);

  const collapsed = new Set(overflow);

  const browserBanner = browserNotice ? (
    <div className="banner" role="status">
      <p>{browserNotice}</p>
      <button type="button" onClick={() => setBrowserNotice(null)}>
        閉じる
      </button>
    </div>
  ) : null;
  const lossText = idleLossText(session);

  if (access === "blocked") {
    return (
      <div className="app">
        {browserBanner}
        <p className="notice">{SECOND_TAB_MESSAGE}</p>
      </div>
    );
  }

  if (access !== "editing") {
    return (
      <div className="app">
        {browserBanner}
        {error ? <p className="notice">{error}</p> : null}
      </div>
    );
  }

  return (
    <div className="app">
      {browserBanner}
      {leftoverNotice ? (
        <div className="banner" role="status">
          <p>{LEFTOVER_MESSAGE}</p>
          <button type="button" onClick={() => setLeftoverNotice(false)}>
            閉じる
          </button>
        </div>
      ) : null}
      {idleWarning && !warningDismissed ? (
        <div className="banner" role="status">
          <p>
            操作が無いため、まもなく終了します。
            {lossText}
          </p>
          <div className="banner-actions">
            <button type="button" onClick={() => void continueWork()}>
              作業を続ける
            </button>
            <button type="button" data-idle-dismiss onClick={() => setWarningDismissed(true)}>
              閉じる
            </button>
          </div>
        </div>
      ) : null}
      <header className="chrome">
        <nav className="nav">
          <button type="button" className="tool nav-current" aria-current={view === "edit" ? "page" : undefined} onClick={() => openView("edit")}>
            <span className="tool-label">PDF 編集</span>
          </button>
          <button type="button" className="tool" aria-current={view === "settings" ? "page" : undefined} onClick={() => openView("settings")}>
            <span className="tool-label">設定</span>
          </button>
          <button type="button" className="tool" aria-current={view === "help" ? "page" : undefined} onClick={() => openView("help")}>
            <span className="tool-label">ヘルプ</span>
          </button>
        </nav>
      </header>
      <div className="editor" hidden={view !== "edit"} onDragOver={onDragOver} onDrop={onDrop}>
      <header className={iconsOnly ? "toolbar icons-only" : "toolbar"} ref={toolbarRef}>
        <button
          type="button"
          className="tool"
          disabled={loading || busy || picking || dialog !== null || resetting}
          onClick={() => void pickPdfs()}
        >
          <span className="tool-label">PDF を選ぶ</span>
        </button>
        {loading ? (
          <button type="button" onClick={abortLoading}>
            読み込みを中止
          </button>
        ) : null}
        <span data-collapse="exit" hidden={collapsed.has("exit")}>
          <TipButton label="終了" icon="exit" warn onClick={() => void quit()} />
        </span>
        <span data-collapse="start-fresh" hidden={collapsed.has("start-fresh")}>
          <TipButton
            label="新しく始める"
            icon="start"
            warn
            disabled={startFreshReason !== undefined}
            reason={startFreshReason}
            onClick={() => void startFresh()}
          />
        </span>
        <span data-collapse="undo" hidden={collapsed.has("undo")}>
          <TipButton
            label="元に戻す"
            icon="undo"
            disabled={Boolean(editLocked) || !session.undo_enabled}
            title={editLocked ?? session.undo_hint}
            onClick={() => void runCommand({ type: "undo" })}
          />
        </span>
        <span data-collapse="redo" hidden={collapsed.has("redo")}>
          <TipButton
            label="やり直す"
            icon="redo"
            disabled={Boolean(editLocked) || !session.redo_enabled}
            title={editLocked ?? session.redo_hint}
            onClick={() => void runCommand({ type: "redo" })}
          />
        </span>
        <span data-collapse="rotate-left" hidden={collapsed.has("rotate-left")}>
          <TipButton
            label="左回転"
            icon="left"
            disabled={selectionReason !== undefined}
            reason={selectionReason}
            onClick={() => rotateSelection("left")}
          />
        </span>
        <span data-collapse="rotate-right" hidden={collapsed.has("rotate-right")}>
          <TipButton
            label="右回転"
            icon="right"
            disabled={selectionReason !== undefined}
            reason={selectionReason}
            onClick={() => rotateSelection("right")}
          />
        </span>
        <span data-collapse="delete" hidden={collapsed.has("delete")}>
          <TipButton
            label="削除"
            icon="trash"
            warn
            disabled={selectionReason !== undefined}
            reason={selectionReason}
            onClick={deleteSelection}
          />
        </span>
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
        <span className="tool-group" data-collapse="layout" hidden={collapsed.has("layout")}>
          <TipButton
            label="横一列"
            icon="row"
            pressed={settings.thumbnail_layout === "row"}
            disabled={!settingsReady}
            reason="設定の読み込みが終わると使えます。"
            onClick={() => chooseLayout("row")}
          />
          <TipButton
            label="複数列"
            icon="grid"
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
        </span>
        <span data-collapse="clear-splits" hidden={collapsed.has("clear-splits")}>
          <TipButton
            label="すべての分割線を解除"
            icon="clear"
            disabled={clearSplitsReason !== undefined}
            reason={clearSplitsReason}
            onClick={() => void runCommand({ type: "clear_all_splits" })}
          />
        </span>
        <details className="overflow-menu" data-overflow-menu hidden={overflow.length === 0}>
          <summary aria-label="その他の操作">...</summary>
          <div className="menu-panel">
            {collapsed.has("exit") ? (
              <button type="button" className="warn-button" onClick={() => void quit()}>
                終了
              </button>
            ) : null}
            {collapsed.has("start-fresh") ? (
              <button type="button" disabled={startFreshReason !== undefined} onClick={() => void startFresh()}>
                新しく始める
              </button>
            ) : null}
            {collapsed.has("layout") ? (
              <>
                <button type="button" disabled={!settingsReady} onClick={() => chooseLayout("row")}>横一列</button>
                <button type="button" disabled={!settingsReady} onClick={() => chooseLayout("grid")}>複数列</button>
                <button type="button" disabled={!settingsReady} onClick={() => chooseSize("small")}>小</button>
                <button type="button" disabled={!settingsReady} onClick={() => chooseSize("medium")}>中</button>
                <button type="button" disabled={!settingsReady} onClick={() => chooseSize("large")}>大</button>
              </>
            ) : null}
            {collapsed.has("clear-splits") ? (
              <button type="button" disabled={clearSplitsReason !== undefined} onClick={() => void runCommand({ type: "clear_all_splits" })}>
                すべての分割線を解除
              </button>
            ) : null}
            {collapsed.has("redo") ? (
              <button type="button" disabled={Boolean(editLocked) || !session.redo_enabled} onClick={() => void runCommand({ type: "redo" })}>
                やり直す
              </button>
            ) : null}
            {collapsed.has("undo") ? (
              <button type="button" disabled={Boolean(editLocked) || !session.undo_enabled} onClick={() => void runCommand({ type: "undo" })}>
                元に戻す
              </button>
            ) : null}
            {collapsed.has("rotate-left") ? (
              <button type="button" disabled={selectionReason !== undefined} onClick={() => rotateSelection("left")}>左回転</button>
            ) : null}
            {collapsed.has("rotate-right") ? (
              <button type="button" disabled={selectionReason !== undefined} onClick={() => rotateSelection("right")}>右回転</button>
            ) : null}
            {collapsed.has("delete") ? (
              <button type="button" disabled={selectionReason !== undefined} onClick={deleteSelection}>削除</button>
            ) : null}
          </div>
        </details>
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
      {held.map((message) => (
        <p className="notice" key={message}>
          {message}
          <button type="button" onClick={() => setHeld((current) => current.filter((item) => item !== message))}>
            閉じる
          </button>
        </p>
      ))}
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
      </div>
      {view === "settings" ? (
        <SettingsScreen
          settings={settings}
          ready={settingsReady}
          about={about}
          updateNote={updateNote}
          onTheme={(theme) => void saveSettings({ ...settings, theme })}
          onNotifications={(enabled) => void saveSettings({ ...settings, windows_notifications: enabled })}
          onIdle={(minutes) => void saveSettings({ ...settings, idle_minutes: minutes })}
          onReset={() => void resetSettings()}
          onOpenLogs={() => void openLogs()}
          onCheckUpdates={() => setUpdateNote("更新確認先はまだ決まっていません。")}
        />
      ) : null}
      {view === "help" ? <HelpScreen /> : null}
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
