import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";

import { acknowledgeAttentionApiProjectsProjectIdAttentionAckPostMutation } from "../../api/generated/@tanstack/react-query.gen";
import type { AttentionResponse } from "../../api/types";
import {
  ACK_PARAM,
  deliveryState,
  notificationFor,
  currentDelivery,
  readLastNotified,
  recordDelivery,
  shouldNotify,
  subscribeToDelivery,
  writeLastNotified,
  type DeliveryRecord,
  type DeliveryState,
} from "./episode";
import { readEnvironment } from "./push";
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

  // Which episode this mount has already tried, successful or not. It is what stops a
  // refused delivery being retried on every fifteen-second poll, and it is deliberately
  // *not* the durable marker below: a permission the person grants, or a worker that
  // comes back, changes the answer, and a reload is where that gets another chance.
  const attempted = useRef<string | null>(null);

  useEffect(() => {
    if (!attention) return;
    if (!shouldNotify(attention, readLastNotified())) return;
    const note = notificationFor(attention, projectId);
    if (!note) return;
    if (attempted.current === note.episodeId) return;
    attempted.current = note.episodeId;
    void deliver(note).then((outcome) => {
      recordDelivery({ episodeId: note.episodeId, outcome, at: new Date().toISOString() });
      // **The marker records a delivery, not an attempt** (task-421). Writing it first
      // meant a blocked, unsupported or failed attempt consumed the episode exactly as
      // a shown one did, so the one interruption the episode is owed could be spent on
      // a browser that raised nothing -- silently, because the outcome was discarded
      // here too.
      if (outcome === "shown") writeLastNotified(note.episodeId);
    });
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
 * What the page says when a desktop notification cannot be raised.
 *
 * Renders **nothing** when permission is granted and the last attempt was shown. A
 * permanent line confirming that a working thing works is the same mistake as a badge
 * that never reaches zero. A granted permission whose last attempt *failed* is the one
 * state with no other surface, so it gets a line of its own.
 *
 * Renders nothing on a phone or tablet either, where `MobilePush` is the panel that
 * applies. Until task-421's revision both were shown on every device, so a phone was
 * told about Windows notifications and a desktop was offered iPhone Home Screen
 * instructions -- each device carrying the other's advice. Neither panel is a
 * capability claim (a phone can raise a local notification, a desktop can take a push
 * subscription); they are two different answers to "how does AgentJobs reach me when I
 * am not looking at this", and only one of them is the right answer per device.
 *
 * The wording says "desktop" rather than "Windows" because this is served over a
 * tailnet to whatever opens it, and naming the wrong operating system is the same
 * defect in a smaller font.
 *
 * The two failing states are deliberately different sentences. "Blocked" is a decision
 * the person can reverse and the page says where; "not supported" is a browser that
 * cannot, and asking them to look for a setting that is not there would waste their
 * time. Both say what still works, because the badge is the durable half and a person
 * who thinks notifications are the whole feature will believe AgentJobs has gone
 * silent when it has not.
 */
/**
 * The last delivery attempt, watched rather than sampled.
 *
 * Sampled at mount would miss the case it exists for: the attempt happens in the
 * notifier, elsewhere in the tree, at whatever moment an episode opens.
 */
export function useLastDelivery(): DeliveryRecord | null {
  return useSyncExternalStore(
    subscribeToDelivery,
    () => currentDelivery(),
    () => null,
  );
}

export function NotificationDelivery() {
  const [state, request] = useDeliveryState();
  const [handheld] = useState(() => readEnvironment().isHandheld);
  const delivery = useLastDelivery();

  if (handheld) return null;
  if (state === "granted") {
    // Permission is granted and something still went wrong -- a worker that rejected
    // the call, a browser that threw. Nothing else would ever say so: until task-421
    // the outcome was discarded, which is how this feature was silent for two days
    // with every screen looking correct. `blocked` and `unsupported` are not reported
    // here because the two sections below are already their report.
    if (delivery?.outcome !== "failed") return null;
    return (
      <section
        data-testid="attention-delivery"
        data-delivery="failed"
        className="shrink-0 rounded-lg border border-dark-border bg-dark-surface px-4 py-3 text-sm text-dark-muted"
        role="status"
      >
        <strong className="font-semibold text-dark-text">
          The last desktop alert could not be raised.
        </strong>{" "}
        Notifications are allowed, so this is the browser rather than a permission —
        reloading AgentJobs usually settles it. The red badge in the header still tracks
        everything waiting on you.
      </section>
    );
  }

  if (state === "askable") {
    return (
      <section
        data-testid="attention-delivery"
        data-delivery="askable"
        className="shrink-0 rounded-lg border border-blue-500/40 bg-blue-950/20 px-4 py-3 text-sm text-blue-100"
      >
        <strong className="font-semibold">Desktop notifications are off.</strong>{" "}
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
            Desktop notifications are blocked for this site.
          </strong>{" "}
          Allow them in your browser’s site settings — in Chrome, Settings → Privacy and
          security → Site settings → Notifications — to get a desktop alert.
        </>
      ) : (
        <>
          <strong className="font-semibold text-dark-text">
            This browser cannot raise desktop notifications.
          </strong>{" "}
          Open AgentJobs in Chrome, or install it as an app, for desktop alerts.
        </>
      )}{" "}
      The red badge in the header still tracks everything waiting on you.
    </section>
  );
}
