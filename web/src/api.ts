import type { ExportJob, ExportStart, SessionState } from "./types";

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
    throw new Error(body.detail || "処理に失敗しました");
  }
  return body;
}

export function getSession(token: string): Promise<SessionState> {
  return request(token, "/api/session").then((response) => readJson<SessionState>(response));
}

export function postDocument(token: string, file: File): Promise<SessionState> {
  const body = new FormData();
  body.append("file", file);
  return request(token, "/api/documents", { method: "POST", body }).then((response) =>
    readJson<SessionState>(response),
  );
}

export function postExport(token: string): Promise<ExportStart> {
  return request(token, "/api/exports/all", { method: "POST" }).then((response) =>
    readJson<ExportStart>(response),
  );
}

export function getExportJob(token: string, jobId: string): Promise<ExportJob> {
  return request(token, `/api/exports/${jobId}`).then((response) => readJson<ExportJob>(response));
}

export function openSaved(token: string, jobId: string, kind: "open-file" | "open-folder"): Promise<void> {
  return request(token, `/api/exports/${jobId}/${kind}`, { method: "POST" }).then((response) =>
    readJson(response),
  );
}
