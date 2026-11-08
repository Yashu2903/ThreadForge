import { type ReactNode, useEffect, useRef } from "react";

/**
 * An accessible modal: role="dialog" + aria-modal + a label, Escape closes, Tab stays inside, and
 * focus returns to whatever opened it. (Text only; nothing here uses innerHTML.)
 */
export function Dialog(props: { title: string; onClose: () => void; children: ReactNode; busy?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const { onClose, busy } = props;

  useEffect(() => {
    const opener = document.activeElement as HTMLElement | null;
    const root = ref.current;
    const first = root?.querySelector<HTMLElement>("input, button, select, textarea, [tabindex]");
    first?.focus();
    return () => opener?.focus?.();
  }, []);

  function onKeyDown(event: React.KeyboardEvent) {
    if (event.key === "Escape" && !busy) {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== "Tab" || !ref.current) return;
    const focusable = [
      ...ref.current.querySelectorAll<HTMLElement>("input, button, select, textarea, a[href], [tabindex]:not([tabindex='-1'])"),
    ].filter((el) => !el.hasAttribute("disabled"));
    if (focusable.length === 0) return;
    const first = focusable[0]!;
    const last = focusable[focusable.length - 1]!;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="backdrop" onMouseDown={(e) => e.target === e.currentTarget && !busy && onClose()}>
      <div ref={ref} className="dialog" role="dialog" aria-modal="true" aria-label={props.title} onKeyDown={onKeyDown}>
        <h2>{props.title}</h2>
        {props.children}
      </div>
    </div>
  );
}
