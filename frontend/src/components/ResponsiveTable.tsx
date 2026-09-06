import type { HTMLAttributes, TableHTMLAttributes, TdHTMLAttributes } from "react";

/**
 * A column's share of the table, as a CSS length or percentage. `null` means "take
 * whatever is left over", and at most one column in a table should ask for that.
 *
 * These are not decoration. The table is `table-layout: fixed` (see styles.css), which
 * is what stops one long cell from sizing a whole column, and a fixed-layout table
 * takes its widths from a `<colgroup>` or from nothing at all -- and with nothing,
 * every column gets an equal share, which is wrong for a table whose first column
 * holds a two-digit number.
 */
export type ColumnWidth = string | null;

type ResponsiveTableProps = TableHTMLAttributes<HTMLTableElement> & {
  /** One entry per column, in source order. Omit to leave the columns equal. */
  columns?: Array<ColumnWidth>;
  /**
   * Restack into cards when this table's own box is narrow, not only when the window
   * is (task-237).
   *
   * The default rule keys off the viewport, which was the same question until the task
   * list moved into a region a third of the screen wide. A `table-layout: fixed` table
   * whose fixed columns already total more than its box gives the one flexible column
   * what is left over -- nothing -- so the task title collapses to zero pixels and the
   * list renders as a column of blanks. Observed in Chromium at 1280px, where every
   * spec that clicks a task by its title failed with "element is not visible".
   *
   * Opt-in rather than the default because it moves the boundary: a full-width table
   * at an 830px window has a ~780px box, so switching it over would restack a table
   * that fits today. The viewport rule is left governing every other table.
   */
  stackWhenNarrow?: boolean;
};

export function ResponsiveTable({
  className = "",
  columns,
  stackWhenNarrow = false,
  children,
  ...props
}: ResponsiveTableProps) {
  return (
    <div className={`responsive-table-wrap ${stackWhenNarrow ? "responsive-table-scope" : ""}`}>
      <table className={`responsive-table ${className}`} {...props}>
        {columns && (
          <colgroup>
            {columns.map((width, index) => (
              // A column's only identity is its position, which is what the index is.
              // eslint-disable-next-line react/no-array-index-key
              <col key={index} style={width ? { width } : undefined} />
            ))}
          </colgroup>
        )}
        {children}
      </table>
    </div>
  );
}

export function ResponsiveTableRow({
  className = "",
  ...props
}: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={`responsive-table-row ${className}`} {...props} />;
}

type ResponsiveCellProps = TdHTMLAttributes<HTMLTableCellElement> & {
  label: string;
};

export function ResponsiveCell({ label, className = "", ...props }: ResponsiveCellProps) {
  return <td data-label={label} className={`responsive-table-cell ${className}`} {...props} />;
}
