import { useCallback, useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { acknowledgeAttentionApiProjectsProjectIdAttentionAckPostMutation } from "../../api/generated/@tanstack/react-query.gen";
import type { AttentionResponse } from "../../api/types";
import {
  ACK_PARAM,
  deliveryState,
  notificationFor,
  readLastNotified,
  shouldNotify,
  writeLastNotified,
  type DeliveryState,
} from "./episode";
import { applyAppBadge, deliver, paintFavicon } from "./shell";

/**
 * Wiring the attention episode to the Windows shell (task-422).
 *
 * Three separate things, and they fail independently on purpose:
 *
 * * the **taskbar badge and the favicon**, which track the waiting set and are always
 *   safe to attempt;
 * * the **notification**, which is attempted once per episode per browser and may be
 *   refused by a permission or swallowed by Focus Assist;
 * * the **acknowledgment**, which is a person's deliberate act and the only thing that
 *   re-arms the next interruption.
 *
 * Nothing here is the source of truth. The episode lives on the server, and a browser
 * that never opened, was denied permission, or was muted by Windows changes nothing
 * about it -- which is what makes the red badge the durable half and the toast the
 * disposable one.
 */

/** Acknowledge an episode. Fire-and-forget: navigation never waits on it. */
export function useAcknowledgeAttention(projectId: string): (episodeId: string) => void {
  const queryClient = useQueryClient();
  const mutation = useMutation(
    acknowledgeAttentionApiProjectsProjectIdAttentionAckPostMutation(),
  );
  const acknowledged = useRef<Set<string>>(new Set());

  return useCallback(
    (episodeId: string) => {
      if (!episodeId || acknowledged.current.has(episodeId)) return;
      // Remembered per session so the three acts -- badge, toast, opening a member
      // task -- do not each post the same acknowledgment while one is in flight. The
      // server is idempotent about it; this only keeps the network quiet.
      acknowledged.current.add(episodeId);
      mutation.mutate(
        { path: { project_id: projectId }, body: { episode_id: episodeId } },
        {
          onSettled: () => {
            void queryClient.invalidateQueries({
              predicate: (query) =>
                (query.queryKey[0] as { _id?: string } | undefined)?._id ===
                "getAttentionApiProjectsProjectIdAttentionGet",
            });
          },
          onError: () => {
            // Let a later gesture try again. The cost of a lost acknowledgment is one
            // missed re-arm, not a wrong state.
            acknowledged.current.delete(episodeId);
          },
        },
      );
    },
    [mutation, projectId, queryClient],
  );
}

/**
 * Drive the shell from the attention state. Renders nothing.
 *
 * Mounted once inside the project shell rather than per surface, because the badge is
 * a property of the window and two components setting it would fight.
 */
export function AttentionNotifier({
  projectId,
  attention,
}: {
  projectId: string;
  attention: AttentionResponse | null;
}) {
  const count = attention?.blocking ?? 0;
  const episodeId = attention?.episode?.id ?? null;
  const acknowledged = attention?.episode?.acknowledged ?? false;

  useEffect(() => {
    void applyAppBadge(count);
    paintFavicon(count);
  }, [count]);

  useEffect(() => {
    // Cleared on unmount so a shell that goes away does not leave a number on the
    // taskbar that nothing is updating any more.
    return () => {
      void applyAppBadge(0);
      paintFavicon(0);
    };
  }, []);

  useEffect(() => {
    if (!attention) return;
    if (!shouldNotify(attention, readLastNotified())) return;
    const note = notificationFor(attention, projectId);
    if (!note) return;
    // Written before the attempt, not after. A browser that refuses or fails should
    // not retry the same episode on every poll for the rest of the day; the episode is
    // still open, still red, and still says so on the page.
    writeLastNotified(note.episodeId);
    void deliver(note);
    // `acknowledged` is a dependency although it is not read: an episode that is
    // acknowledged and then re-armed arrives with a new id, but an episode
    // acknowledged in another window must stop this one re-evaluating as if new.
  }, [attention, projectId, episodeId, acknowledged]);

  return null;
}

/**
 * Acknowledge the episode a notification click carried, and take the marker off the URL.
 *
 * The click may arrive at a window that did not exist a moment ago, so the episode
 * travels in the query string rather than in a message to a page. Removing it again
 * matters: a bookmarked or reloaded URL would otherwise acknowledge an episode every
 * time it was opened, which is exactly the silent-acknowledgment failure the rule
 * forbids.
 */
export function useAcknowledgeFromUrl(acknowledge: (episodeId: string) => void) {
  const [searchParams, setSearchParams] = useSearchParams();

  useEffect(() => {
    const episodeId = searchParams.get(ACK_PARAM);
    if (!episodeId) return;
    acknowledge(episodeId);
    const remaining = new URLSearchParams(searchParams);
    remaining.delete(ACK_PARAM);
    setSearchParams(remaining, { replace: true });
  }, [acknowledge, searchParams, setSearchParams]);
}

/**
 * Opening a task the episode names is the third acknowledging act.
 *
 * The one that costs nothing to perform and is hardest to perform by accident: you
 * have gone and looked at the work. It is checked against the episode's membership
 * rather than against "any task", so wandering through the backlog is not an
 * acknowledgment -- and against the *episode*, not the current waiting set, so opening
 * a task whose wait began after the alert does not retroactively acknowledge it.
 */
export function useAcknowledgeOnOpen(
  attention: AttentionResponse | null,
  openTaskId: string | null,
  acknowledge: (episodeId: string) => void,
) {
  const episode = attention?.episode;
  const episodeId = episode?.id ?? null;
  const isMember = Boolean(openTaskId && episode?.tasks.includes(openTaskId));

  useEffect(() => {
    if (!episodeId || !isMember) return;
    acknowledge(episodeId);
  }, [acknowledge, episodeId, isMember]);
}

/**
 * Whether this browser can raise a Windows notification, watched rather than sampled.
 *
 * Sampled once at mount would be wrong the moment the person answers the prompt, which
 * is the one time the value changes and the one time the page must stop telling them
 * to enable it.
 */
export function useDeliveryState(): [DeliveryState, () => void] {
  const [state, setState] = useState<DeliveryState>(() =>
    deliveryState(typeof window !== "undefined" && "Notification" in window ? Notification : undefined),
  );

  const request = useCallback(() => {
    if (typeof window === "undefined" || !("Notification" in window)) return;
    // Must happen inside the click. Chrome refuses a permission prompt that is not a
    // response to a gesture, and a refused prompt looks exactly like a denial.
    //
    // The *resolved* value rather than re-reading `Notification.permission`: the
    // promise is the answer to this prompt, while the global is a property whose
    // update a browser is free to schedule however it likes.
    void Notification.requestPermission().then((permission) => {
      setState(deliveryState({ permission }));
    });
  }, []);

  return [state, request];
}

/**
 * What the page says when a Windows notification cannot be raised.
 *
 * Renders **nothing** when permission is granted. A permanent line confirming that a
 * working thing works is the same mistake as a badge that never reaches zero.
 *
 * The two failing states are deliberately different sentences. "Blocked" is a decision
 * the person can reverse and the page says where; "not supported" is a browser that
 * cannot, and asking them to look for a setting that is not there would waste their
 * time. Both say what still works, because the badge is the durable half and a person
 * who thinks notifications are the whole feature will believe AgentJobs has gone
 * silent when it has not.
 */
export function NotificationDelivery() {
  const [state, request] = useDeliveryState();

  if (state === "granted") return null;

  if (state === "askable") {
    return (
      <section
        data-testid="attention-delivery"
        data-delivery="askable"
        className="shrink-0 rounded-lg border border-blue-500/40 bg-blue-950/20 px-4 py-3 text-sm text-blue-100"
      >
        <strong className="font-semibold">Windows notifications are off.</strong>{" "}
        AgentJobs can raise a desktop alert the first time work stops on you, so you do
        not have to keep this window in view.
        <button
          type="button"
          data-testid="enable-notifications"
          onClick={request}
          className="touch-target ml-3 rounded-md border border-blue-400/60 px-3 text-xs font-semibold text-blue-100 hover:bg-blue-500/20"
        >
          Turn on notifications
        </button>
      </section>
    );
  }

  return (
    <section
      data-testid="attention-delivery"
      data-delivery={state}
      className="shrink-0 rounded-lg border border-dark-border bg-dark-surface px-4 py-3 text-sm text-dark-muted"
      role="status"
    >
      {state === "denied" ? (
        <>
          <strong className="font-semibold text-dark-text">
            Windows notifications are blocked for this site.
          </strong>{" "}
          Allow them in Chrome under Settings → Privacy and security → Site settings →
          Notifications to get a desktop alert.
        </>
      ) : (
        <>
          <strong className="font-semibold text-dark-text">
            This browser cannot raise Windows notifications.
          </strong>{" "}
          Open AgentJobs in Chrome, or install it as an app, for desktop alerts.
        </>
      )}{" "}
      The red badge in the header still tracks everything waiting on you.
    </section>
  );
}
