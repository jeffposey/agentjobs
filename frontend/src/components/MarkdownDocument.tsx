import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * A document under review, rendered as Markdown (task-594).
 *
 * The first thing in this app to render agent-authored text as Markdown, so the rules
 * that keep it safe are stated rather than left to the library's defaults:
 *
 * 1. **No raw HTML.** `react-markdown` builds React elements from a syntax tree and never
 *    reaches `innerHTML`; without `rehype-raw` an HTML block in the source is dropped.
 * 2. **Only absolute web links are anchors.** Its default `urlTransform` already strips
 *    `javascript:` and friends. A relative link (`../design.md`, `#section`) would
 *    resolve against the dashboard's own URL and open some unrelated screen, so it is
 *    shown as text with its target on hover instead.
 * 3. **No images.** A remote image is a request to an address an agent chose, made from
 *    the reviewer's browser, and a relative one cannot resolve. The alt text stands in.
 *
 * Tables and code blocks scroll inside their own box: a design doc's decision table is
 * wider than a phone, and the page itself must never scroll sideways.
 *
 * Its own module so `TaskDetail` can load it lazily: the parser is most of the weight
 * this task adds to the bundle, and nobody who never opens a document should pay for it.
 */
const ABSOLUTE = /^(https?:|mailto:)/i;

const components: Components = {
  h1: (props) => <h1 className="mt-6 mb-3 text-xl font-bold text-dark-text first:mt-0" {...strip(props)} />,
  h2: (props) => <h2 className="mt-6 mb-2 text-lg font-semibold text-dark-text first:mt-0" {...strip(props)} />,
  h3: (props) => <h3 className="mt-5 mb-2 font-semibold text-dark-text first:mt-0" {...strip(props)} />,
  h4: (props) => <h4 className="mt-4 mb-1 font-semibold text-dark-text" {...strip(props)} />,
  h5: (props) => <h5 className="mt-4 mb-1 font-semibold text-dark-text" {...strip(props)} />,
  h6: (props) => <h6 className="mt-4 mb-1 font-semibold text-dark-muted" {...strip(props)} />,
  p: (props) => <p className="my-3" {...strip(props)} />,
  ul: (props) => <ul className="my-3 list-disc space-y-1 pl-6" {...strip(props)} />,
  ol: (props) => <ol className="my-3 list-decimal space-y-1 pl-6" {...strip(props)} />,
  blockquote: (props) => <blockquote className="my-3 border-l-4 border-dark-border pl-4 text-dark-muted" {...strip(props)} />,
  hr: () => <hr className="my-6 border-dark-border" />,
  pre: (props) => <pre className="my-3 overflow-x-auto rounded-lg bg-dark-bg p-3 text-xs leading-5" {...strip(props)} />,
  code: (props) => <code className="rounded bg-dark-bg px-1 font-mono text-[0.85em]" {...strip(props)} />,
  table: (props) => (
    <div className="my-3 overflow-x-auto">
      <table className="min-w-full border-collapse text-left text-sm" {...strip(props)} />
    </div>
  ),
  th: (props) => <th className="border border-dark-border px-2 py-1 align-top font-semibold" {...strip(props)} />,
  td: (props) => <td className="border border-dark-border px-2 py-1 align-top" {...strip(props)} />,
  a: ({ href, children }) =>
    href && ABSOLUTE.test(href) ? (
      <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue-300 underline hover:text-blue-200">
        {children}
      </a>
    ) : (
      <span className="underline decoration-dotted" title={href ?? undefined}>
        {children}
      </span>
    ),
  img: ({ alt }) => <span className="text-dark-muted">[image{alt ? `: ${alt}` : ""}]</span>,
};

/** Drop react-markdown's `node` prop, which is not a DOM attribute. */
function strip<T extends { node?: unknown }>(props: T): Omit<T, "node"> {
  const { node: _node, ...rest } = props;
  return rest;
}

export default function MarkdownDocument({ text }: { text: string }) {
  return (
    <div className="markdown-document break-words text-sm leading-6 text-dark-text">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </Markdown>
    </div>
  );
}
