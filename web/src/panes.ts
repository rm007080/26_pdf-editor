import type { ThumbnailSize } from "./types";

export const FRAME_HEIGHT: Record<ThumbnailSize, number> = {
  small: 96,
  medium: 150,
  large: 220,
};

export const THUMB_CHROME = 96;
export const PREVIEW_MIN = 240;
export const THUMB_MIN = 160;
export const SPLITTER = 8;

export function oneRowHeight(size: ThumbnailSize): number {
  return FRAME_HEIGHT[size] + THUMB_CHROME;
}

function fitPane(available: number, preferredThumbs: number): { preview: number; thumbs: number } {
  let thumbs = preferredThumbs;
  let preview = available - thumbs;
  if (available >= PREVIEW_MIN + THUMB_MIN) {
    if (preview < PREVIEW_MIN) {
      preview = PREVIEW_MIN;
      thumbs = available - preview;
    }
    if (thumbs < THUMB_MIN) {
      thumbs = THUMB_MIN;
      preview = available - thumbs;
    }
  }
  return { preview: Math.max(0, preview), thumbs: Math.max(0, thumbs) };
}

export function paneHeights(
  available: number,
  ratio: number | null,
  rowHeight: number,
): { preview: number; thumbs: number } {
  if (available <= 0) {
    return { preview: PREVIEW_MIN, thumbs: Math.max(THUMB_MIN, rowHeight) };
  }
  if (ratio == null) {
    return fitPane(available, Math.max(THUMB_MIN, rowHeight));
  }
  return fitPane(available, available * ratio);
}

export function ratioFromThumb(available: number, thumbHeight: number): number | null {
  if (available <= 0) {
    return null;
  }
  const fitted = fitPane(available, thumbHeight);
  const ratio = fitted.thumbs / (fitted.preview + fitted.thumbs);
  if (!(ratio > 0 && ratio < 1) || !Number.isFinite(ratio)) {
    return null;
  }
  return Math.min(0.9999, Math.max(0.0001, Math.round(ratio * 10000) / 10000));
}
