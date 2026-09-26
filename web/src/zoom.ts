export const ZOOM_STEPS = [
  25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300, 325, 350, 375, 400,
] as const;

export type NamedZoom = "fit-page" | "fit-width" | "actual";

export type ZoomState = { kind: "named"; mode: NamedZoom } | { kind: "step"; percent: number };

export type ZoomMetrics = {
  effective: number;
  fitPage: number;
  fitWidth: number;
};

export function initialZoom(): ZoomState {
  return { kind: "named", mode: "fit-page" };
}

export function effectivePercent(state: ZoomState, fitPage: number, fitWidth: number): number {
  if (state.kind === "step") {
    return state.percent;
  }
  if (state.mode === "fit-width") {
    return fitWidth;
  }
  if (state.mode === "actual") {
    return 100;
  }
  return fitPage;
}

function sameZoom(left: ZoomState, right: ZoomState): boolean {
  if (left.kind === "step" && right.kind === "step") {
    return left.percent === right.percent;
  }
  if (left.kind === "named" && right.kind === "named") {
    return left.mode === right.mode;
  }
  return false;
}

export function changeZoom(state: ZoomState, effective: number, direction: 1 | -1): ZoomState {
  if (direction > 0) {
    if (state.kind === "named" && effective < 25) {
      return { kind: "step", percent: 25 };
    }
    const next = ZOOM_STEPS.find((step) => step > effective + 1e-6);
    if (next === undefined) {
      return state;
    }
    return { kind: "step", percent: next };
  }
  if (state.kind === "named" && effective < 25) {
    return state;
  }
  const next = [...ZOOM_STEPS].reverse().find((step) => step < effective - 1e-6);
  if (next === undefined) {
    return state;
  }
  return { kind: "step", percent: next };
}

export function zoomUnchanged(state: ZoomState, effective: number, direction: 1 | -1): boolean {
  return sameZoom(state, changeZoom(state, effective, direction));
}

export function formatPercent(value: number): string {
  const rounded = Math.round(value * 10) / 10;
  if (Number.isInteger(rounded)) {
    return `${rounded}%`;
  }
  return `${rounded.toFixed(1)}%`;
}
