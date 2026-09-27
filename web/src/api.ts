import type {
  CommandResult,
  DocumentIntake,
  EditCommand,
  ExportJob,
  ExportStart,
  OpenPdfsResult,
  SessionState,
  SplitBegin,
  ViewSettings,
} from "./types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
  }
}

export function readToken(): string {
  const content = document.querySelector('meta[name="pdf-editor-token"]')?.getAttribute("content") ?? "";
  if (!content || content === "__PDF_EDITOR_TOKEN__") {
    throw new Error("画面のトークンを読めませんでした");
  }
  return content;
}

const TAB_KEY = "pdf-editor-tab-id";

export function tabId(): string {
  const existing = sessionStorage.getItem(TAB_KEY);
  if (existing) {
    return existing;
  }
  const created = crypto.randomUUID();
  sessionStorage.setItem(TAB_KEY, created);
  return created;
}

async function request(token: string, path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
  headers.set("X-Tab-Id", tabId());
  return fetch(path, { ...init, headers });
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T & { detail?: string };
  if (!response.ok) {
    throw new ApiError(response.status, body.detail || "処理に失敗しました");
  }
  return body;
}

export function getSettings(token: string): Promise<ViewSettings> {
  return request(token, "/api/settings").then((response) => readJson<ViewSettings>(response));
}

export function putSettings(token: string, settings: ViewSettings): Promise<ViewSettings> {
  const body = {
    thumbnail_layout: settings.thumbnail_layout,
    thumbnail_size: settings.thumbnail_size,
    pane_ratio: settings.pane_ratio,
    theme: settings.theme,
    windows_notifications: settings.windows_notifications,
    idle_minutes: settings.idle_minutes,
  };
  return request(token, "/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }).then((response) => readJson<ViewSettings>(response));
}

export function postSettingsReset(token: string): Promise<ViewSettings> {
  return request(token, "/api/settings/reset", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: "{}",
  }).then((response) => readJson<ViewSettings>(response));
}

export function postOpenPdfs(token: string): Promise<OpenPdfsResult> {
  return request(token, "/api/dialogs/open-pdfs", { method: "POST" }).then((response) =>
    readJson<OpenPdfsResult>(response),
  );
}

export function postDuplicate(
  token: string,
  documentId: string,
  approvedDocumentIds: string[],
): Promise<{ duplicate: boolean }> {
  return request(token, `/api/documents/${documentId}/duplicate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ approved_document_ids: approvedDocumentIds }),
  }).then((response) => readJson<{ duplicate: boolean }>(response));
}

export function getAbout(token: string): Promise<{ name: string; version: string; cleared_leftover: boolean }> {
  return request(token, "/api/about").then((response) =>
    readJson<{ name: string; version: string; cleared_leftover: boolean }>(response),
  );
}

export function postClaim(token: string): Promise<Record<string, never>> {
  return request(token, "/api/editor/claim", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab_id: tabId() }),
  }).then((response) => readJson<Record<string, never>>(response));
}

export function postHeartbeat(token: string): Promise<{ idle_warning: boolean }> {
  return request(token, "/api/editor/heartbeat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tab_id: tabId() }),
  }).then((response) => readJson<{ idle_warning: boolean }>(response));
}

export function postActivity(
  token: string,
  source: "click" | "key" | "scroll" | "settings" | "continue",
): Promise<{ reset: boolean }> {
  return request(token, "/api/activity", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source }),
  }).then((response) => readJson<{ reset: boolean }>(response));
}

export function postShutdown(token: string, confirmed: boolean): Promise<{ stopped: boolean }> {
  return request(token, "/api/shutdown", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirmed }),
  }).then((response) => readJson<{ stopped: boolean }>(response));
}

export function postLogsOpen(token: string): Promise<{ status: string }> {
  return request(token, "/api/logs/open", { method: "POST" }).then((response) =>
    readJson<{ status: string }>(response),
  );
}

export function postCommand(token: string, command: EditCommand): Promise<CommandResult> {
  return request(token, "/api/session/commands", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(command),
  }).then((response) => readJson<CommandResult>(response));
}

export function patchSelection(
  token: string,
  selectedPageIds: string[],
  activePageId: string | null,
): Promise<SessionState> {
  return request(token, "/api/session/selection", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ selected_page_ids: selectedPageIds, active_page_id: activePageId }),
  }).then((response) => readJson<SessionState>(response));
}

export function getSession(token: string): Promise<SessionState> {
  return request(token, "/api/session").then((response) => readJson<SessionState>(response));
}

export function postSessionReset(token: string): Promise<SessionState> {
  return request(token, "/api/session/reset", { method: "POST" }).then((response) =>
    readJson<SessionState>(response),
  );
}

export function postDocument(token: string, file: File): Promise<DocumentIntake> {
  const body = new FormData();
  body.append("file", file);
  return request(token, "/api/documents", { method: "POST", body }).then((response) =>
    readJson<DocumentIntake>(response),
  );
}

export function postPassword(token: string, documentId: string, password: string): Promise<DocumentIntake> {
  return request(token, `/api/documents/${documentId}/password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  }).then((response) => readJson<DocumentIntake>(response));
}

export function discardDocuments(token: string, documentIds: string[]): Promise<{ discarded: string[] }> {
  return request(token, "/api/documents/discard", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ document_ids: documentIds }),
  }).then((response) => readJson<{ discarded: string[] }>(response));
}

export function postExport(token: string): Promise<ExportStart> {
  return request(token, "/api/exports/all", { method: "POST" }).then((response) =>
    readJson<ExportStart>(response),
  );
}

export function postSelectionExport(token: string): Promise<ExportStart> {
  return request(token, "/api/exports/selection", { method: "POST" }).then((response) =>
    readJson<ExportStart>(response),
  );
}

export function postSplit(token: string): Promise<SplitBegin> {
  return request(token, "/api/exports/split", { method: "POST" }).then((response) =>
    readJson<SplitBegin>(response),
  );
}

export function postSplitConfirm(token: string, names: string[]): Promise<{ job_id: string }> {
  return request(token, "/api/exports/split/confirm", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ names }),
  }).then((response) => readJson<{ job_id: string }>(response));
}

export function postSplitCancel(token: string): Promise<{ status: string }> {
  return request(token, "/api/exports/split/cancel", { method: "POST" }).then((response) =>
    readJson<{ status: string }>(response),
  );
}

export function getExportJob(token: string, jobId: string): Promise<ExportJob> {
  return request(token, `/api/exports/${jobId}`).then((response) => readJson<ExportJob>(response));
}

export function openSaved(
  token: string,
  jobId: string,
  kind: "open-file" | "open-folder",
  index?: number,
): Promise<void> {
  const init: RequestInit = { method: "POST" };
  if (index !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify({ index });
  }
  return request(token, `/api/exports/${jobId}/${kind}`, init).then((response) => readJson(response));
}
