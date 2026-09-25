import { type MouseEvent, type ReactNode, type RefObject, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { Maximize2, X } from "lucide-react";

/**
 * Read one thing with the whole screen, then go back to exactly where you were.
 *
 * Added for documents under review (task-594), where a design doc squeezed into the
 * review card is readable but cramped, and written to be reused: the owner has asked
 * for the same on a run's session output.
 *
 * - **It covers the app rather than navigating.** No route changes, so closing it is
 *   not "back" and cannot lose the task page's scroll position or an open composer.
 * - **Two ways out, both obvious**: the X in the corner and Escape. Focus goes to the X
 *   on open and back to `returnFocus` on close -- named rather than read off
 *   `document.activeElement`, because Safari does not focus a button it was clicked on.
 * - **The page underneath does not scroll** while it is open; only the view does.
 * - **A portal onto `document.body`**, so no ancestor's `overflow` or `@container` can
 *   clip a `fixed` element -- the review card has both.
 */
export function FullscreenView({
  title,
  subtitle,
  onClose,
  returnFocus,
  children,
}: {
  title: string;
  subtitle?: ReactNode;
  onClose: () => void;
  returnFocus?: RefObject<HTMLElement | null>;
  children: ReactNode;
}) {
  const close = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    const opener = returnFocus;
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    close.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      opener?.current?.focus();
    };
  }, [onClose, returnFocus]);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={title}
      className="fixed inset-0 z-50 flex flex-col bg-dark-bg"
    >
      <header className="flex items-center gap-3 border-b border-dark-border bg-dark-surface px-4 py-2">
        <div className="min-w-0 flex-1">
          <p className="truncate font-mono text-sm text-dark-text">{title}</p>
          {subtitle && <p className="truncate text-xs text-dark-muted">{subtitle}</p>}
        </div>
        <button
          ref={close}
          type="button"
          onClick={onClose}
          aria-label="Close full screen"
          title="Close (Esc)"
          className="touch-target flex items-center justify-center rounded-lg text-dark-muted hover:bg-dark-border hover:text-dark-text"
        >
          <X aria-hidden="true" className="h-6 w-6" />
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[78ch] px-4 py-6">{children}</div>
      </div>
    </div>,
    document.body,
  );
}

/** The icon button that opens a {@link FullscreenView}. `label` names what it maximizes. */
export function MaximizeButton({
  label,
  onClick,
  ref,
}: {
  label: string;
  onClick: (event: MouseEvent<HTMLButtonElement>) => void;
  ref?: RefObject<HTMLButtonElement | null>;
}) {
  return (
    <button
      ref={ref}
      type="button"
      onClick={onClick}
      aria-label={label}
      title={label}
      className="touch-target flex shrink-0 items-center justify-center rounded-lg text-dark-muted hover:bg-dark-border hover:text-dark-text"
    >
      <Maximize2 aria-hidden="true" className="h-5 w-5" />
    </button>
  );
}
