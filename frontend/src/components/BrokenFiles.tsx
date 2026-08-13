import type { BrokenTaskFile } from "../api/generated";

export function BrokenFiles({ files }: { files: Array<BrokenTaskFile> }) {
  if (files.length === 0) return null;
  return (
    <section className="rounded-lg border-2 border-red-600/60 bg-red-950/40 p-4" aria-label="Unreadable task files">
      <h2 className="flex items-center gap-2 font-semibold text-red-300">
        <span aria-hidden="true">⚠️</span> {files.length} task file{files.length === 1 ? "" : "s"} could not be loaded
      </h2>
      <ul className="mt-2 space-y-1 text-sm">
        {files.map((file) => <li key={file.path}><span className="font-mono text-red-300">{file.filename}</span><span className="text-dark-muted"> — {file.reason}</span></li>)}
      </ul>
      <p className="mt-2 text-xs text-dark-muted">These are not shown below. Fix the file, or the task stays invisible.</p>
    </section>
  );
}
