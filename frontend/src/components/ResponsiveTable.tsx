import type { HTMLAttributes, TableHTMLAttributes, TdHTMLAttributes } from "react";

export function ResponsiveTable({
  className = "",
  ...props
}: TableHTMLAttributes<HTMLTableElement>) {
  return (
    <div className="responsive-table-wrap">
      <table className={`responsive-table ${className}`} {...props} />
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
