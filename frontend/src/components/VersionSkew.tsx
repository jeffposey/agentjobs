import { useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiVersionApiVersionGetOptions } from "../api/generated/@tanstack/react-query.gen";
import { BUNDLE_API_DIGEST } from "../api/apiDigest";
import { describeSkew, type Skew } from "../api/skew";

/**
 * Says when this page is not the build the server it is talking to expects.
 *
 * Rendered beside the router rather than inside a page, like the issue reporter: skew
 * is a property of the tab, not of whatever route happens to be open, and the pages
 * that render before a project resolves are exactly the ones a broken contract is
 * likely to break first.
 *
 * **It never blocks anything.** No banner is shown until a poll has succeeded and
 * disagreed, so an unreachable server, a slow one, or one too old to report a digest
 * all leave the app exactly as it was. A stale server is usually still mostly usable,
 * and locking somebody out of their tracker to tell them about a version mismatch
 * trades one problem for a worse one.
 *
 * It also stops short of reloading by itself. The tab may hold a half-written note or
 * a form somebody is in the middle of, and taking that away without asking is not an
 * improvement on saying so and offering the button.
 */

/**
 * How often to ask. Slow on purpose: skew is created by a human running a build or a
 * restart, so the event this watches for happens on the timescale of minutes, and the
 * answer costs a schema digest the server has already computed and cached.
 */
export const VERSION_POLL_MS = 60_000;

function Banner({ skew, onDismiss }: { skew: Skew; onDismiss: () => void }) {
  const contract = skew.kind === "contract";
  return (
    <div
      className={
        "fixed bottom-20 left-4 right-4 z-50 rounded-xl border p-4 shadow-lg sm:bottom-4 sm:right-auto sm:max-w-md " +
        (contract
          ? "border-orange-500/60 bg-orange-950/90 text-orange-50"
          : "border-blue-500/60 bg-blue-950/90 text-blue-50")
      }
      role={contract ? "alert" : "status"}
    >
      <p className="text-sm font-semibold">{skew.title}</p>
      <p className="mt-2 text-sm">{skew.detail}</p>
      {skew.remedies.length > 0 && (
        <ul className="mt-2 list-disc space-y-1 pl-5 text-sm">
          {skew.remedies.map((remedy) => (
            <li key={remedy}>{remedy}</li>
          ))}
        </ul>
      )}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          className="touch-target rounded-lg border border-current px-3 text-sm font-semibold"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
        <button
          type="button"
          className="touch-target rounded-lg px-3 text-sm underline"
          onClick={onDismiss}
        >
          Dismiss
        </button>
      </div>
    </div>
  );
}

export function VersionSkew() {
  const [dismissed, setDismissed] = useState<string | null>(null);
  /**
   * The bundle id this tab is running.
   *
   * Taken from the first successful poll rather than compiled in, because the id is a
   * hash of the built assets and therefore does not exist until after the build that
   * produced them. The first poll happens within a second of the page loading its own
   * assets, so the value it reads is the build those assets came from -- with a race
   * that requires a rebuild to land inside that second, and whose consequence is one
   * missed reload prompt rather than a wrong one.
   */
  const tabBundleId = useRef<string | null>(null);

  const versionQuery = useQuery({
    ...apiVersionApiVersionGetOptions(),
    refetchInterval: VERSION_POLL_MS,
    refetchOnWindowFocus: true,
    retry: false,
    staleTime: 0,
  });

  const version = versionQuery.data;
  const servedBundleId = typeof version?.bundle_id === "string" ? version.bundle_id : null;
  if (servedBundleId && tabBundleId.current === null) tabBundleId.current = servedBundleId;

  const skew = describeSkew({
    bundleDigest: BUNDLE_API_DIGEST,
    // Typed as a string by the generated client, but a server older than this field
    // omits it -- which is the one case a skew detector must not turn into a fault.
    serverDigest: typeof version?.api_digest === "string" ? version.api_digest : null,
    tabBundleId: tabBundleId.current,
    servedBundleId,
  });

  if (!skew || skew.fingerprint === dismissed) return null;
  return <Banner skew={skew} onDismiss={() => setDismissed(skew.fingerprint)} />;
}
