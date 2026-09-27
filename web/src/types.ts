export type PageRecord = {
  id: string;
  document_id: string;
  source_page: number;
  order: number;
  deleted: boolean;
  rotation: 0 | 90 | 180 | 270;
};

export type DocumentRecord = {
  id: string;
  filename: string;
};

export type SessionState = {
  documents: DocumentRecord[];
  pages: PageRecord[];
  selected_page_ids: string[];
  active_page_id: string | null;
  undo_enabled: boolean;
  redo_enabled: boolean;
  undo_hint: string;
  redo_hint: string;
  unsaved: boolean;
};

export type CommandResult = SessionState & {
  changed: boolean;
  history_dropped: boolean;
};

export type DocumentIntake = {
  document_id: string;
  needs_password: boolean;
  byte_size: number;
  page_count: number | null;
  signed: boolean;
  duplicate: boolean;
};

export type EditCommand =
  | { type: "rotate"; page_ids: string[]; direction: "left" | "right" }
  | { type: "delete"; page_ids: string[] }
  | { type: "reorder"; page_ids: string[]; before_page_id?: string; after_page_id?: string }
  | { type: "append_documents"; document_ids: string[] }
  | { type: "undo" }
  | { type: "redo" };

export type ThumbnailLayout = "row" | "grid";
export type ThumbnailSize = "small" | "medium" | "large";

export type ViewSettings = {
  thumbnail_layout: ThumbnailLayout;
  thumbnail_size: ThumbnailSize;
  pane_ratio: number | null;
};

export type ExportStage = "copying" | "verifying" | "committing" | "completed" | "failed";

export type ExportJob = {
  job_id: string;
  stage: ExportStage;
  processed_pages: number;
  total_pages: number;
  ratio: number;
  path: string | null;
  detail: string | null;
};

export type ExportStart =
  | { status: "cancelled" }
  | { job_id: string };
