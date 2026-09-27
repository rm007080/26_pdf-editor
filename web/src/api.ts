import type {
  CommandResult,
  DocumentIntake,
  EditCommand,
  ExportJob,
  ExportStart,
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

async function request(token: string, path: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${token}`);
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
  return request(token, "/api/settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(settings),
  }).then((response) => readJson<ViewSettings>(response));
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
