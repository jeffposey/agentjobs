/**
 * A picture of the row that follows a finger during a touch reorder (task-589).
 *
 * With a mouse the browser draws this itself: the grip's `dragstart` hands the row to
 * `setDragImage`, and the browser's drag controller paints it under the pointer. That
 * exists only inside an HTML5 drag, and a finger's drag is not one -- it runs on pointer
 * events, where the browser paints nothing. So the same picture is made here instead:
 * a copy of the row, fixed to the viewport, moved by the finger.
 *
 * The copy is taken when the drag starts, before the list re-renders the source row
 * faded, so -- as with the mouse -- it is a picture of the row as it reads rather than
 * of the hole it leaves.
 */

export type DragGhost = {
  /** Put the ghost where a finger at viewport point (x, y) holds it. */
  moveTo: (x: number, y: number) => void;
  remove: () => void;
};

/**
 * Attributes a copy must not carry. A second element with the grip's `id` would make
 * `getElementById` and every label lookup ambiguous, and a second `data-task` would be
 * counted as a row by anything that lists them.
 */
const STRIPPED = ["id", "data-task", "data-dragging", "data-drop-side", "draggable"];

export function startDragGhost(row: HTMLElement, x: number, y: number): DragGhost {
  const box = row.getBoundingClientRect();
  const copy = row.cloneNode(true) as HTMLElement;
  for (const element of [copy, ...copy.querySelectorAll<HTMLElement>("*")]) {
    for (const name of STRIPPED) element.removeAttribute(name);
  }

  // A `<tr>` means nothing outside a table, so the wide table's row is carried in one.
  let ghost: HTMLElement = copy;
  if (row.tagName === "TR") {
    const table = document.createElement("table");
    table.className = row.closest("table")?.className ?? "";
    const body = document.createElement("tbody");
    body.appendChild(copy);
    table.appendChild(body);
    ghost = table;
  }

  ghost.setAttribute("data-drag-ghost", "true");
  ghost.setAttribute("aria-hidden", "true");
  Object.assign(ghost.style, {
    position: "fixed",
    left: "0px",
    top: "0px",
    width: `${box.width}px`,
    margin: "0",
    zIndex: "60",
    pointerEvents: "none",
    listStyle: "none",
  });

  // Held where the finger took it, so the row does not jump to put its corner there.
  const offsetX = x - box.left;
  const offsetY = y - box.top;
  const moveTo = (atX: number, atY: number) => {
    ghost.style.transform = `translate(${atX - offsetX}px, ${atY - offsetY}px)`;
  };
  moveTo(x, y);
  document.body.appendChild(ghost);

  return { moveTo, remove: () => ghost.remove() };
}
