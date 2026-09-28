import { useEffect, useRef, type ReactNode, type RefObject } from "react";

function positionMenu(menu: HTMLDetailsElement) {
  if (!menu.open) return;
  const summary = menu.querySelector("summary")!;
  const panel = menu.querySelector<HTMLElement>(".menu-panel")!;
  const anchor = summary.getBoundingClientRect();
  const margin = 8;
  const gap = 6;
  const below = window.innerHeight - anchor.bottom - gap - margin;
  const above = anchor.top - gap - margin;
  if (anchor.bottom < margin || anchor.top > window.innerHeight - margin) {
    menu.open = false;
    return;
  }
  const opensBelow = below >= Math.min(panel.scrollHeight, 240) || below >= above;
  panel.style.maxHeight = `${Math.max(0, opensBelow ? below : above)}px`;
  panel.style.left = `${Math.max(margin, Math.min(anchor.left, window.innerWidth - panel.offsetWidth - margin))}px`;
  panel.style.top = `${opensBelow ? anchor.bottom + gap : Math.max(margin, anchor.top - gap - panel.offsetHeight)}px`;
}

export function ToolbarMenu({ label, disabled = false, menuRef, children }: {
  label: string;
  disabled?: boolean;
  menuRef?: RefObject<HTMLDetailsElement | null>;
  children: ReactNode;
}) {
  const localRef = useRef<HTMLDetailsElement>(null);
  const ref = menuRef ?? localRef;

  useEffect(() => {
    const update = () => { if (ref.current) positionMenu(ref.current); };
    const closeOutside = (event: PointerEvent) => {
      const menu = ref.current;
      if (menu?.open && event.target instanceof Node && !menu.contains(event.target)) {
        menu.open = false;
      }
    };
    window.addEventListener("resize", update);
    document.addEventListener("scroll", update, { capture: true, passive: true });
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      window.removeEventListener("resize", update);
      document.removeEventListener("scroll", update, true);
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [ref]);

  return (
    <details className="toolbar-menu" ref={ref}
      onToggle={(event) => positionMenu(event.currentTarget)}
      onKeyDown={(event) => {
        if (event.key === "Escape" && event.currentTarget.open) {
          event.stopPropagation();
          event.currentTarget.open = false;
          event.currentTarget.querySelector("summary")?.focus();
        }
      }}
      onClick={(event) => {
        if (event.target instanceof Element && event.target.closest("button:not(:disabled)")) {
          event.currentTarget.open = false;
        }
      }}>
      <summary aria-disabled={disabled} onClick={(event) => { if (disabled) event.preventDefault(); }}>
        {label}<span aria-hidden="true">▾</span>
      </summary>
      <div className="menu-panel">{children}</div>
    </details>
  );
}
