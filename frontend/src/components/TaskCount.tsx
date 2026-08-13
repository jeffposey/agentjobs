export function TaskCount({ count }: { count: number }) {
  return (
    <p className="text-dark-text">
      The scoped API returned <strong>{count}</strong> {count === 1 ? "task" : "tasks"}.
    </p>
  );
}
