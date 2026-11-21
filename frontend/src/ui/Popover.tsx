/**
 * Popovers, menus and tooltips for the whole application. They are rendered where they are used (no portal) with
 * `position: fixed` coordinates taken from the trigger, so they are never clipped by a scrolling panel, they always stay
 * inside the visible window (they flip above the trigger when there is no room below, and are clamped to the sides), and the
 * DOM order keeps the keyboard order natural.
 *
 *   Menu     a list of actions (role="menu"): Up/Down/Home/End move, Enter/Space choose, Escape closes and returns focus to the
 *            trigger, Tab leaves, a click outside closes.
 *   Popover  any content in a panel with the same placement and closing rules (the toolbar's overflow, the attach panel).
 *   Tip      a tooltip that shows on hover AND keyboard focus (a `title` attribute does neither reliably).
 */
import { type CSSProperties, type ReactNode, useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";

export interface MenuItem {
  id: string;
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  danger?: boolean;
  disabled?: boolean;
  hidden?: boolean;
  separatorBefore?: boolean;
  hint?: string;
}

type Align = "start" | "end";

interface Placement {
  top: number;
  left: number;
  maxHeight: number;
}

const GAP = 6;
const MARGIN = 8;

/** Where the panel goes: below the trigger, or above when that fits better, clamped to the window. */
export function place(trigger: DOMRect, panel: { width: number; height: number }, align: Align, vw: number, vh: number): Placement {
  const spaceBelow = vh - trigger.bottom - GAP - MARGIN;
  const spaceAbove = trigger.top - GAP - MARGIN;
  const below = panel.height <= spaceBelow || spaceBelow >= spaceAbove;
  const room = below ? spaceBelow : spaceAbove;
  const height = Math.min(panel.height, Math.max(120, room));
  const top = below ? trigger.bottom + GAP : trigger.top - GAP - height;
  let left = align === "end" ? trigger.right - panel.width : trigger.left;
  left = Math.max(MARGIN, Math.min(left, vw - panel.width - MARGIN));
  return { top: Math.max(MARGIN, top), left, maxHeight: Math.max(120, room) };
}

function usePopover(onClose?: () => void) {
  const [open, setOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const [style, setStyle] = useState<CSSProperties>({ visibility: "hidden" });
  const close = useCallback(
    (refocus = true) => {
      setOpen(false);
      onClose?.();
      if (refocus) triggerRef.current?.focus();
    },
    [onClose],
  );

  const reposition = useCallback(
    (align: Align) => {
      const t = triggerRef.current;
      const p = panelRef.current;
      if (!t || !p) return;
      const placement = place(t.getBoundingClientRect(), { width: p.offsetWidth, height: p.scrollHeight }, align, window.innerWidth, window.innerHeight);
      setStyle({ position: "fixed", top: placement.top, left: placement.left, maxHeight: placement.maxHeight, visibility: "visible" });
    },
    [],
  );

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: PointerEvent) => {
      const target = e.target as Node;
      if (panelRef.current?.contains(target) || triggerRef.current?.contains(target)) return;
      close(false);
    };
    document.addEventListener("pointerdown", onPointer, true);
    return () => document.removeEventListener("pointerdown", onPointer, true);
  }, [open, close]);

  return { open, setOpen, triggerRef, panelRef, style, setStyle, close, reposition };
}

export function Popover(props: {
  label: string;
  trigger: ReactNode;
  children: (close: () => void) => ReactNode;
  align?: Align;
  className?: string;
  triggerClassName?: string;
  testId?: string;
  panelLabel?: string;
  disabled?: boolean;
  onOpenChange?: (open: boolean) => void;
}) {
  const align = props.align ?? "start";
  const pop = usePopover(() => props.onOpenChange?.(false));
  const id = useId();
  useLayoutEffect(() => {
    if (!pop.open) {
      pop.setStyle({ visibility: "hidden" });
      return;
    }
    pop.reposition(align);
    const on = () => pop.reposition(align);
    window.addEventListener("resize", on);
    window.addEventListener("scroll", on, true);
    return () => {
      window.removeEventListener("resize", on);
      window.removeEventListener("scroll", on, true);
    };
  }, [pop.open, align]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <span className={`popover-host ${props.className ?? ""}`}>
      <button
        ref={pop.triggerRef}
        type="button"
        className={props.triggerClassName}
        aria-haspopup="dialog"
        aria-expanded={pop.open}
        aria-controls={pop.open ? id : undefined}
        aria-label={props.label}
        data-testid={props.testId}
        disabled={props.disabled}
        onMouseDown={(e) => e.preventDefault()}
        onClick={() => {
          pop.setOpen(!pop.open);
          props.onOpenChange?.(!pop.open);
        }}
      >
        {props.trigger}
      </button>
      {pop.open && (
        <div
          ref={pop.panelRef}
          id={id}
          className="popover-panel"
          role="dialog"
          aria-label={props.panelLabel ?? props.label}
          style={pop.style}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              pop.close();
            }
          }}
        >
          {props.children(() => pop.close())}
        </div>
      )}
    </span>
  );
}

export function Menu(props: {
  label: string;
  trigger: ReactNode;
  items: MenuItem[];
  align?: Align;
  className?: string;
  triggerClassName?: string;
  testId?: string;
  disabled?: boolean;
}) {
  const align = props.align ?? "start";
  const pop = usePopover();
  const id = useId();
  const items = props.items.filter((i) => !i.hidden);
  const [active, setActive] = useState(0);

  useLayoutEffect(() => {
    if (!pop.open) {
      pop.setStyle({ visibility: "hidden" });
      return;
    }
    pop.reposition(align);
    const on = () => pop.reposition(align);
    window.addEventListener("resize", on);
    window.addEventListener("scroll", on, true);
    return () => {
      window.removeEventListener("resize", on);
      window.removeEventListener("scroll", on, true);
    };
  }, [pop.open, align]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!pop.open) return;
    const enabled = pop.panelRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])');
    enabled?.[0]?.focus();
    setActive(0);
  }, [pop.open]); // eslint-disable-line react-hooks/exhaustive-deps

  function onKeyDown(e: React.KeyboardEvent) {
    const nodes = [...(pop.panelRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([aria-disabled="true"])') ?? [])];
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      pop.close();
      return;
    }
    if (e.key === "Tab") {
      pop.close(false);
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key) || nodes.length === 0) return;
    e.preventDefault();
    const at = nodes.indexOf(document.activeElement as HTMLElement);
    const next = e.key === "Home" ? 0 : e.key === "End" ? nodes.length - 1 : (at + (e.key === "ArrowDown" ? 1 : -1) + nodes.length) % nodes.length;
    setActive(next);
    nodes[next]!.focus();
  }

  return (
    <span className={`popover-host ${props.className ?? ""}`}>
      <button
        ref={pop.triggerRef}
        type="button"
        className={props.triggerClassName}
        aria-haspopup="menu"
        aria-expanded={pop.open}
        aria-controls={pop.open ? id : undefined}
        aria-label={props.label}
        data-testid={props.testId}
        disabled={props.disabled}
        onClick={() => pop.setOpen(!pop.open)}
        onKeyDown={(e) => {
          if (!pop.open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            pop.setOpen(true);
          }
        }}
      >
        {props.trigger}
      </button>
      {pop.open && (
        <div ref={pop.panelRef} id={id} className="popover-panel menu-panel" role="menu" aria-label={props.label} style={pop.style} onKeyDown={onKeyDown}>
          {items.map((item, i) => (
            <div key={item.id} role="none">
              {item.separatorBefore && i > 0 && <hr className="menu-sep" />}
              <button
                type="button"
                role="menuitem"
                tabIndex={i === active ? 0 : -1}
                className={`menu-item${item.danger ? " danger" : ""}`}
                aria-disabled={item.disabled ? true : undefined}
                onClick={() => {
                  if (item.disabled) return;
                  pop.close();
                  item.onSelect();
                }}
              >
                {item.icon && <span className="menu-icon">{item.icon}</span>}
                <span className="menu-label">{item.label}</span>
                {item.hint && <small className="menu-hint">{item.hint}</small>}
              </button>
            </div>
          ))}
          {items.length === 0 && <p className="muted small menu-empty">Nothing to do here.</p>}
        </div>
      )}
    </span>
  );
}

/** A tooltip on hover and on keyboard focus. Wrap exactly one focusable child. */
export function Tip(props: { label: string; children: ReactNode; side?: "right" | "bottom" | "top" }) {
  const [at, setAt] = useState<{ x: number; y: number } | null>(null);
  const side = props.side ?? "right";
  const show = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    if (side === "right") setAt({ x: r.right + 10, y: r.top + r.height / 2 });
    else if (side === "bottom") setAt({ x: r.left + r.width / 2, y: r.bottom + 8 });
    else setAt({ x: r.left + r.width / 2, y: r.top - 8 });
  };
  return (
    <span
      className="tip-host"
      onMouseEnter={(e) => show(e.currentTarget)}
      onMouseLeave={() => setAt(null)}
      onFocus={(e) => show(e.currentTarget)}
      onBlur={() => setAt(null)}
      onKeyDown={(e) => e.key === "Escape" && setAt(null)}
    >
      {props.children}
      {at && (
        <span role="tooltip" className={`tip tip-${side}`} style={{ left: at.x, top: at.y }}>
          {props.label}
        </span>
      )}
    </span>
  );
}
