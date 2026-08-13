type ConnectionUnavailableProps = {
  offline: boolean;
};

export function ConnectionUnavailable({ offline }: ConnectionUnavailableProps) {
  return (
    <main className="mx-auto flex min-h-screen max-w-xl items-center px-4 py-10">
      <section className="w-full rounded-2xl border border-orange-500/50 bg-dark-surface p-6" role="alert">
        <p className="text-sm font-semibold uppercase tracking-[0.2em] text-orange-300">
          {offline ? "Offline" : "Server unavailable"}
        </p>
        <h1 className="mt-3 text-2xl font-bold text-dark-text">
          {offline ? "You’re offline" : "AgentJobs cannot be reached"}
        </h1>
        <p className="mt-3 text-dark-muted">
          No task data is shown because cached assignments could be out of date.
        </p>
        <p className="mt-2 text-sm text-dark-muted">
          Reconnect to the network and wake the computer running AgentJobs, then reload.
        </p>
      </section>
    </main>
  );
}
