import { useCallback, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import type { SpecDraftResponse } from "../api/generated";
import {
  draftTaskSpecApiProjectsProjectIdModelDraftPostMutation,
  getModelStatusApiModelGetOptions,
} from "../api/generated/@tanstack/react-query.gen";
import {
  type AppliedDraft,
  type DraftableValues,
  changedAnything,
  nameFields,
} from "../report/specDraft";

/**
 * The drafting control, shared by the create form and the issue reporter.
 *
 * One component for both because the two are the same interaction and would otherwise
 * drift into two: a checkbox that is **checked by default**, a button that draws one
 * draft, a banner that says what it touched, and an undo. The sequence the spec asks for
 * is two steps with a human between them -- type, press, read, edit, create -- and the
 * create button is not touched by anything in this file.
 *
 * **The checkbox arms the control rather than arming the create button**, which is the
 * one reading of "on by default" that keeps `Create` meaning exactly what it means
 * today. The alternative -- draft on submit -- would make the button that files a task
 * sometimes not file one, and a control that changes what the primary action does is
 * worse than no control.
 *
 * **When no model is configured the checkbox is present, disabled and unchecked**, with
 * the reason where its hint goes. That is the design's "absent or disabled with one
 * sentence saying why" (§6), resolved towards disabled: the explanation then sits where
 * a person looks for the control, and the form behaves in every other respect exactly as
 * it does today.
 */

type SpecDraftControlProps = {
  projectId: string;
  /** Read out of the form at press time, so a draft compares against what is there now. */
  readValues: () => DraftableValues;
  /** What the person typed, which is what the model is given. */
  readInput: () => { title: string; description: string };
  /** Apply the result. The parent owns the form, so it decides how fields are written. */
  onApply: (draft: SpecDraftResponse, values: DraftableValues) => AppliedDraft;
  /** Put back exactly what was in the form before the draft landed. */
  onUndo: (values: DraftableValues) => void;
  /**
   * Lift the checkbox, so one control governs both things drafting can mean (task-121).
   *
   * Uncontrolled and on by default when absent, which is every surface but the capture
   * dialog. The dialog passes it because a finding added to the tray is fleshed out
   * *without* anyone pressing the button below -- and a checkbox reading "flesh this out
   * with AI" that the control beside it ignores is worse than no checkbox at all.
   */
  enabled?: boolean;
  onEnabledChange?: (enabled: boolean) => void;
  /** True where a tray is present, which changes what checking this box promises. */
  collecting?: boolean;
};

export function SpecDraftControl({
  projectId,
  readValues,
  readInput,
  onApply,
  onUndo,
  enabled: controlledEnabled,
  onEnabledChange,
  collecting = false,
}: SpecDraftControlProps) {
  const status = useQuery(getModelStatusApiModelGetOptions());
  const draft = useMutation(draftTaskSpecApiProjectsProjectIdModelDraftPostMutation());

  const [ownEnabled, setOwnEnabled] = useState(true);
  const enabled = controlledEnabled ?? ownEnabled;
  const setEnabled = (next: boolean) => {
    setOwnEnabled(next);
    onEnabledChange?.(next);
  };
  const [applied, setApplied] = useState<AppliedDraft | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const abort = useRef<AbortController | null>(null);

  const available = status.data?.available === true;
  const unavailable = status.data?.detail ?? null;

  const run = useCallback(async () => {
    setError(null);
    setMessage(null);
    setApplied(null);
    const controller = new AbortController();
    abort.current = controller;
    const input = readInput();
    try {
      const result = await draft.mutateAsync({
        path: { project_id: projectId },
        body: { title: input.title, description: input.description },
        signal: controller.signal,
      });
      if (controller.signal.aborted) return;
      if (!result.drafted) {
        // A refusal is a 200 carrying its reason, so it reads here as "no draft, here
        // is why" rather than going down the same path as a failed save.
        setError(result.detail ?? "No draft was produced.");
        return;
      }
      const outcome = onApply(result, readValues());
      if (!changedAnything(outcome)) {
        setMessage("The draft matched what you had already written; nothing changed.");
        return;
      }
      setApplied(outcome);
    } catch (caught) {
      if (controller.signal.aborted) return;
      setError(
        caught instanceof Error && caught.name === "AbortError"
          ? null
          : "The draft could not be fetched. The form still works; write it yourself or try again.",
      );
    } finally {
      abort.current = null;
    }
  }, [draft, onApply, projectId, readInput, readValues]);

  const cancel = () => {
    abort.current?.abort();
    abort.current = null;
    setError(null);
  };

  const undo = () => {
    if (!applied) return;
    onUndo(applied.previous);
    setApplied(null);
    setMessage("Put back what you had written.");
  };

  return (
    <section
      aria-labelledby="spec-draft-heading"
      className="rounded-lg border border-dark-border bg-dark-surface p-5"
    >
      <h3 id="spec-draft-heading" className="sr-only">
        Draft the specification with AI
      </h3>

      <label className="flex items-start gap-3 font-medium">
        <input
          type="checkbox"
          className="mt-1"
          checked={available && enabled}
          disabled={!available}
          onChange={(event) => setEnabled(event.target.checked)}
        />
        <span>
          Flesh this out with AI before I file it
          <span className="mt-1 block text-xs font-normal text-dark-muted">
            {!available
              ? (unavailable ??
                "Drafting is unavailable on this machine, so write the specification yourself.")
              : collecting
                ? "A model expands what you wrote into a full spec. Press the button to draft this one into the fields below and read it first; anything you add to the list is fleshed out on its own, filling only what you left blank."
                : "A model expands what you wrote into a full spec and puts the draft in these fields. You read it, fix it, and press Create — nothing is filed until you do."}
          </span>
        </span>
      </label>

      {available && enabled && (
        <div className="mobile-action-row mt-4 flex items-center gap-3">
          <button
            type="button"
            onClick={() => void run()}
            disabled={draft.isPending}
            className="touch-target rounded-lg border border-blue-500/60 px-4 font-semibold text-blue-200 hover:bg-blue-950/40 disabled:opacity-60"
          >
            {draft.isPending ? "Drafting…" : "Draft the spec"}
          </button>
          {draft.isPending && (
            <>
              <span role="status" className="text-sm text-dark-muted">
                Asking {status.data?.model ?? "the model"}… the form stays usable.
              </span>
              <button
                type="button"
                onClick={cancel}
                className="touch-target rounded-lg px-3 text-sm font-semibold text-dark-muted hover:bg-dark-border"
              >
                Cancel
              </button>
            </>
          )}
        </div>
      )}

      {error && (
        <p
          role="alert"
          className="mt-4 rounded-lg border border-amber-500/60 bg-amber-950/40 p-3 text-sm text-amber-200"
        >
          {error}
        </p>
      )}

      {message && !applied && (
        <p role="status" className="mt-4 text-sm text-dark-muted">
          {message}
        </p>
      )}

      {applied && (
        <div
          role="status"
          className="mt-4 rounded-lg border border-blue-500/60 bg-blue-950/30 p-3 text-sm text-blue-100"
        >
          {applied.filled.length > 0 && <p>Filled {nameFields(applied.filled)}.</p>}
          {applied.replaced.length > 0 && (
            <p className="font-semibold text-amber-200">
              Replaced what you had written in {nameFields(applied.replaced)}.
            </p>
          )}
          <p className="mt-1 text-blue-200/80">
            Read it before you file it. Nothing has been saved.
          </p>
          <button
            type="button"
            onClick={undo}
            className="touch-target mt-2 rounded-lg border border-blue-400/60 px-3 font-semibold text-blue-100 hover:bg-blue-900/40"
          >
            Undo the draft
          </button>
        </div>
      )}
    </section>
  );
}
