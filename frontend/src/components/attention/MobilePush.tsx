import { useCallback, useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  getPushStatusApiProjectsProjectIdPushGetOptions,
  sendTestPushApiProjectsProjectIdPushTestPostMutation,
  subscribePushDeviceApiProjectsProjectIdPushSubscribePostMutation,
  unsubscribePushDeviceApiProjectsProjectIdPushUnsubscribePostMutation,
} from "../../api/generated/@tanstack/react-query.gen";
import type { PushDeviceView, PushStatusResponse } from "../../api/types";
import {
  clearPushContext,
  deviceLabel,
  pushAvailability,
  readDeviceId,
  readEnvironment,
  subscribePayload,
  writeDeviceId,
  writePushContext,
  type PushAvailability,
} from "./push";

/**
 * Opting a phone in, and seeing whether it is still working (task-423).
 *
 * Beside the desktop `NotificationDelivery` notice rather than on a settings page of
 * its own, because they are two answers to one question -- how does AgentJobs reach me
 * when I am not looking at this -- and separating them would make the phone half the
 * one nobody finds.
 *
 * Nothing here decides when to interrupt. The episode, the acknowledgment and the
 * reset are the server's, shared with the desktop notifier; a phone is another client
 * of the same episode, and all this panel does is register one and report on it.
 */

/** What this browser's own push subscription is, watched rather than assumed. */
function useThisDevice(): {
  endpoint: string | null;
  json: { endpoint: string; keys?: { p256dh?: string; auth?: string } } | null;
  refresh: () => void;
} {
  const [json, setJson] = useState<{
    endpoint: string;
    keys?: { p256dh?: string; auth?: string };
  } | null>(null);
  const [nonce, setNonce] = useState(0);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        if (typeof navigator === "undefined" || !("serviceWorker" in navigator)) return;
        const registration = await navigator.serviceWorker.ready;
        const subscription = await registration.pushManager?.getSubscription();
        if (!cancelled) {
          setJson(
            subscription
              ? (subscription.toJSON() as { endpoint: string; keys?: Record<string, string> })
              : null,
          );
        }
      } catch {
        // A browser with no push at all answers here rather than throwing into render.
        if (!cancelled) setJson(null);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [nonce]);

  return { endpoint: json?.endpoint ?? null, json, refresh: () => setNonce((n) => n + 1) };
}

function statusLine(device: PushDeviceView): string {
  if (!device.healthy) {
    return `not reachable — ${device.consecutive_failures} failures in a row${
      device.last_error ? `: ${device.last_error}` : ""
    }`;
  }
  if (device.last_status && device.last_status >= 400) {
    return `last attempt ${device.last_status}${device.last_error ? ` — ${device.last_error}` : ""}`;
  }
  if (device.last_attempt_at) return "delivering";
  return "waiting for the first alert";
}

/**
 * Registering *this* device for push, shown only on a phone or tablet.
 *
 * The gate is `isHandheld`, and it is about which answer applies rather than what the
 * browser can do -- a desktop Chrome will take a push subscription perfectly well. On a
 * desktop the local notification `NotificationDelivery` offers is the better answer to
 * the same question and arrives without a round trip through a push service, so
 * offering both there is two ways to do one thing. Before task-421's revision this
 * panel rendered everywhere, which is how a Windows desktop came to be given
 * instructions for adding AgentJobs to an iPhone Home Screen.
 */
export function MobilePush({ projectId }: { projectId: string }) {
  const queryClient = useQueryClient();
  const [handheld] = useState(() => readEnvironment().isHandheld);
  const [availability, setAvailability] = useState<PushAvailability>(() =>
    pushAvailability(readEnvironment()),
  );
  const [problem, setProblem] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [tested, setTested] = useState<string | null>(null);
  const device = useThisDevice();

  const status = useQuery({
    ...getPushStatusApiProjectsProjectIdPushGetOptions({ path: { project_id: projectId } }),
    // The key is what a subscription is created against, and the device list changes
    // only when somebody presses a button here. Refetching it on a timer would be
    // polling for an event that cannot happen without this component knowing.
    staleTime: 60_000,
    retry: false,
  });
  const body = status.data as PushStatusResponse | undefined;
  const devices = useMemo(() => body?.devices ?? [], [body]);

  const subscribeMutation = useMutation(
    subscribePushDeviceApiProjectsProjectIdPushSubscribePostMutation(),
  );
  const unsubscribeMutation = useMutation(
    unsubscribePushDeviceApiProjectsProjectIdPushUnsubscribePostMutation(),
  );
  const testMutation = useMutation(sendTestPushApiProjectsProjectIdPushTestPostMutation());

  const refresh = useCallback(() => {
    void queryClient.invalidateQueries({
      predicate: (query) =>
        (query.queryKey[0] as { _id?: string } | undefined)?._id ===
        "getPushStatusApiProjectsProjectIdPushGet",
    });
  }, [queryClient]);

  const enable = useCallback(async () => {
    setProblem(null);
    setBusy(true);
    try {
      // Inside the click, always. Chrome and Safari both refuse a permission prompt
      // that is not a response to a gesture, and a refused prompt is indistinguishable
      // from a denial afterwards.
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        setAvailability(pushAvailability(readEnvironment()));
        setProblem(
          permission === "denied"
            ? "Notifications are blocked for this site. Allow them in your browser's site settings and try again."
            : "Notifications were not allowed, so nothing was registered.",
        );
        return;
      }
      const key = body?.vapid_public_key;
      if (!key) {
        setProblem("AgentJobs has no application key yet. Reload and try again.");
        return;
      }
      const registration = await navigator.serviceWorker.ready;
      const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: key,
      });
      const label = deviceLabel(navigator.userAgent);
      const payload = subscribePayload(
        subscription.toJSON() as { endpoint: string; keys?: Record<string, string> },
        { label },
      );
      if (!payload) {
        setProblem("This browser returned a subscription without keys, which cannot be used.");
        return;
      }
      await subscribeMutation.mutateAsync({
        path: { project_id: projectId },
        body: payload,
      });
      await writePushContext({
        projectId,
        applicationServerKey: key,
        label,
        detail: "count",
      });
      device.refresh();
      refresh();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "Could not register this device.");
    } finally {
      setBusy(false);
    }
  }, [body, device, projectId, refresh, subscribeMutation]);

  const disable = useCallback(async () => {
    setBusy(true);
    setProblem(null);
    try {
      const endpoint = device.endpoint;
      if (typeof navigator !== "undefined" && "serviceWorker" in navigator) {
        const registration = await navigator.serviceWorker.ready;
        const subscription = await registration.pushManager?.getSubscription();
        // The browser first, then the server. The other order leaves a window in which
        // the browser is still subscribed to an endpoint nothing will ever send to.
        await subscription?.unsubscribe();
      }
      if (endpoint) {
        await unsubscribeMutation.mutateAsync({
          path: { project_id: projectId },
          body: { endpoint },
        });
      }
      await clearPushContext();
      writeDeviceId(null);
      device.refresh();
      refresh();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : "Could not unregister this device.");
    } finally {
      setBusy(false);
    }
  }, [device, projectId, refresh, unsubscribeMutation]);

  const forget = useCallback(
    async (subscriptionId: string) => {
      setBusy(true);
      try {
        await unsubscribeMutation.mutateAsync({
          path: { project_id: projectId },
          body: { subscription_id: subscriptionId },
        });
        if (readDeviceId() === subscriptionId) writeDeviceId(null);
        refresh();
      } finally {
        setBusy(false);
      }
    },
    [projectId, refresh, unsubscribeMutation],
  );

  const test = useCallback(async () => {
    setBusy(true);
    setTested(null);
    try {
      const answer = await testMutation.mutateAsync({
        path: { project_id: projectId },
        body: {},
      });
      const results = answer?.results ?? [];
      const sent = results.filter((row) => row.outcome === "sent").length;
      setTested(
        results.length === 0
          ? "No devices are registered, so nothing was sent."
          : `${sent} of ${results.length} delivered${
              sent === results.length
                ? "."
                : ` — ${results.find((row) => row.outcome !== "sent")?.error ?? "see the list"}`
            }`,
      );
      refresh();
    } catch (error) {
      setTested(error instanceof Error ? error.message : "The test could not be sent.");
    } finally {
      setBusy(false);
    }
  }, [projectId, refresh, testMutation]);

  if (!handheld) return null;

  // A read that 403s is a run asking, which is the capability boundary working. Say
  // nothing rather than render a refusal into a person's Dashboard.
  if (status.isError) return null;

  const registeredHere = Boolean(device.endpoint) && devices.length > 0;
  const others = registeredHere ? devices.length - 1 : devices.length;

  return (
    <section
      data-testid="mobile-push"
      data-availability={availability}
      data-registered={registeredHere ? "yes" : "no"}
      className="shrink-0 rounded-lg border border-dark-border bg-dark-surface px-4 py-3 text-sm text-dark-muted"
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <strong className="font-semibold text-dark-text">Phone notifications</strong>
        {availability === "unsupported" && (
          <span>This browser cannot receive push notifications.</span>
        )}
        {availability === "install-required" && (
          <span data-testid="push-install-required">
            On iPhone and iPad, add AgentJobs to your Home Screen first — Safari’s Share menu,
            then “Add to Home Screen”. Push is only available to an installed web app there.
          </span>
        )}
        {availability === "blocked" && (
          <span>
            Notifications are blocked for this site. Allow them in your browser’s site settings,
            then reload.
          </span>
        )}
        {availability === "available" && !registeredHere && (
          <>
            <span>Get woken on your phone the first time work stops on you.</span>
            <button
              type="button"
              data-testid="enable-push"
              // Disabled until the key has arrived. The button renders as soon as the
              // browser's own capabilities are known, which is before the first
              // response, and a click in that window could only fail.
              disabled={busy || status.isPending}
              onClick={() => void enable()}
              className="touch-target rounded-md border border-blue-400/60 px-3 text-xs font-semibold text-blue-100 hover:bg-blue-500/20 disabled:opacity-60"
            >
              {busy ? "Registering…" : "Turn on for this device"}
            </button>
          </>
        )}
        {availability === "available" && registeredHere && (
          <>
            <span data-testid="push-registered">
              On for this device{others > 0 ? ` and ${others} other${others === 1 ? "" : "s"}` : ""}.
            </span>
            <button
              type="button"
              data-testid="test-push"
              disabled={busy}
              onClick={() => void test()}
              className="touch-target rounded-md border border-dark-border px-3 text-xs font-semibold text-dark-text hover:border-blue-500/60 disabled:opacity-60"
            >
              Send a test
            </button>
            <button
              type="button"
              data-testid="disable-push"
              disabled={busy}
              onClick={() => void disable()}
              className="touch-target rounded-md border border-dark-border px-3 text-xs text-dark-muted hover:border-red-700/70 hover:text-red-200 disabled:opacity-60"
            >
              Turn off
            </button>
          </>
        )}
      </div>

      {body && !body.watching && (
        <p data-testid="push-not-watching" className="mt-2 text-amber-200">
          This server is not currently watching for new attention, so no push will be sent.
          Restart AgentJobs.
        </p>
      )}
      {problem && (
        <p data-testid="push-problem" className="mt-2 text-red-300">
          {problem}
        </p>
      )}
      {tested && (
        <p data-testid="push-test-result" className="mt-2 text-dark-text">
          {tested}
        </p>
      )}

      {devices.length > 0 && (
        <ul className="mt-2 space-y-1" data-testid="push-devices">
          {devices.map((row) => (
            <li key={row.id} className="flex flex-wrap items-center gap-x-2 text-xs">
              <span className={row.healthy ? "text-dark-text" : "text-red-300"}>
                {row.label || "Unnamed device"}
              </span>
              <span className="text-dark-muted">{row.service}</span>
              <span className="text-dark-muted">· {statusLine(row)}</span>
              <button
                type="button"
                data-testid={`forget-push-${row.id}`}
                disabled={busy}
                onClick={() => void forget(row.id)}
                className="touch-target rounded border border-dark-border px-2 text-dark-muted hover:border-red-700/70 hover:text-red-200 disabled:opacity-60"
              >
                Forget
              </button>
            </li>
          ))}
        </ul>
      )}

      <p className="mt-2 text-xs">
        A push says how many tasks are waiting and nothing about what they are. The complete
        ask is on the task.
      </p>
    </section>
  );
}
