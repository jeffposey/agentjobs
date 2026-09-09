/**
 * Deciding whether this page and the server answering it are the same build.
 *
 * Kept as a pure function on purpose: the interesting cases are combinations of four
 * identifiers, and asserting on them through a rendered component would mean building
 * four servers to ask four questions. The component decides how to say it; this
 * decides whether there is anything to say.
 *
 * Two independent skews, because they have different causes and different remedies:
 *
 * - **The bundle in this tab is not the bundle being served.** A rebuild landed while
 *   the tab stayed open, which is most rebuilds -- the great majority change no API
 *   route at all. One reload fixes it, and until somebody reloads the tab keeps
 *   running the old code with no sign that it is doing so.
 * - **The contract this bundle was generated from is not the one the server serves.**
 *   The generated client's types are then a promise about responses nobody is keeping,
 *   which surfaces as a crash somewhere unrelated. A reload does not fix this one; the
 *   server is running older code, or the bundle was never rebuilt.
 *
 * The bundle case is checked first, because when both are true the reload is the
 * cheaper thing to try and may well settle the second as well.
 */

export type SkewKind = "bundle" | "contract";

export type Skew = {
  kind: SkewKind;
  title: string;
  detail: string;
  /** What fixes it, in the order to try. Rendered as a list; may be empty. */
  remedies: string[];
  /**
   * Identifies *this* mismatch, so dismissing it hides this one and not the next.
   * A banner that stays dismissed after the situation changes is a banner that lied.
   */
  fingerprint: string;
};

export type SkewInputs = {
  /** The contract digest compiled into this bundle. */
  bundleDigest: string;
  /** What the server reports now. Null when unreachable, or older than this field. */
  serverDigest?: string | null;
  /** The served bundle id this tab first saw -- effectively the build it is running. */
  tabBundleId?: string | null;
  /** The bundle id the server is serving now. Null when it has no bundle or no id. */
  servedBundleId?: string | null;
};

/** Enough of a digest to tell two apart in a sentence, without filling the banner. */
export function shortDigest(digest: string): string {
  return digest.slice(0, 12);
}

export function describeSkew({
  bundleDigest,
  serverDigest,
  tabBundleId,
  servedBundleId,
}: SkewInputs): Skew | null {
  if (tabBundleId && servedBundleId && tabBundleId !== servedBundleId) {
    return {
      kind: "bundle",
      title: "A newer build of AgentJobs is being served",
      detail:
        `This tab is still running the build it loaded (${tabBundleId}); the server is ` +
        `now serving ${servedBundleId}. Reload to pick it up.`,
      remedies: [],
      fingerprint: `bundle:${tabBundleId}:${servedBundleId}`,
    };
  }

  // Only compare when the server said something. An older server has no digest to
  // report and an unreachable one has said nothing at all; neither is evidence of
  // skew, and treating "cannot tell" as "out of step" would put a permanent banner on
  // every install that has not upgraded yet.
  if (serverDigest && serverDigest !== bundleDigest) {
    return {
      kind: "contract",
      title: "This page and the server disagree about the API",
      detail:
        `This page was built against API contract ${shortDigest(bundleDigest)}; the ` +
        `server is serving ${shortDigest(serverDigest)}. Responses may not be the ` +
        `shape this page expects, so parts of it can fail in ways that look unrelated.`,
      remedies: [
        "Restart the server so it runs the code on disk: agentjobs restart.",
        "If it survives the restart, this bundle is the stale half: run npm run build in frontend/, then reload. frontend_dist/ is gitignored, so pulling and restarting never rebuilds it.",
      ],
      fingerprint: `contract:${bundleDigest}:${serverDigest}`,
    };
  }

  return null;
}
