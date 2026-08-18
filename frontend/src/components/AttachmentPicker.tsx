import { useId, useState } from "react";

import {
  AttachmentRejected,
  imagesFromTransfer,
  readImage,
  type PendingAttachment,
} from "../report/attachments";

/**
 * The paste target shared by the two surfaces that take prose from a person.
 *
 * The interaction the spec cares about is: take a screenshot, click into the box,
 * Ctrl+V, submit. So the paste handler lives on the textarea itself and nothing about
 * attaching requires finding a control first. Drag-and-drop and a file input are
 * fallbacks, present because a picker is occasionally the only way, and deliberately
 * secondary in the layout.
 *
 * A rejected image never disturbs the prose. That is the failure ac-6 names: losing
 * what someone typed because their screenshot was too big is worse than not supporting
 * screenshots at all.
 */

type AttachmentPickerProps = {
  label: string;
  hint?: string;
  value: string;
  onChange: (value: string) => void;
  attachments: Array<PendingAttachment>;
  onAttachmentsChange: (attachments: Array<PendingAttachment>) => void;
  textareaClassName: string;
  name?: string;
  required?: boolean;
  autoFocus?: boolean;
};

export function AttachmentPicker({
  label,
  hint,
  value,
  onChange,
  attachments,
  onAttachmentsChange,
  textareaClassName,
  name,
  required,
  autoFocus,
}: AttachmentPickerProps) {
  const [error, setError] = useState<string | null>(null);
  const fileInputId = useId();

  const accept = async (files: Array<File>) => {
    if (files.length === 0) return;
    setError(null);
    const accepted: Array<PendingAttachment> = [];
    for (const file of files) {
      try {
        accepted.push(await readImage(file));
      } catch (caught) {
        // Reported and skipped, one image at a time: a batch where one file is too
        // large should still attach the others rather than discard the lot.
        setError(
          caught instanceof AttachmentRejected
            ? caught.message
            : "That image could not be attached. Your text is untouched.",
        );
      }
    }
    if (accepted.length > 0) onAttachmentsChange([...attachments, ...accepted]);
  };

  return (
    <div className="space-y-2">
      <label className="block font-medium">
        {label}
        {hint && <span className="mt-1 block text-xs font-normal text-dark-muted">{hint}</span>}
        <textarea
          // Named explicitly, so the accessible name stays the label alone rather than
          // absorbing the hint and the paste instructions wrapped in the same element.
          aria-label={label}
          name={name}
          required={required}
          autoFocus={autoFocus}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onPaste={(event) => {
            const images = imagesFromTransfer(event.clipboardData);
            if (images.length === 0) return;
            // Only when the clipboard actually held an image, so pasting text still
            // pastes text.
            event.preventDefault();
            void accept(images);
          }}
          onDragOver={(event) => event.preventDefault()}
          onDrop={(event) => {
            const images = imagesFromTransfer(event.dataTransfer);
            if (images.length === 0) return;
            event.preventDefault();
            void accept(images);
          }}
          className={textareaClassName}
        />
      </label>

      <p className="text-xs text-dark-muted">
        Paste a screenshot with Ctrl+V, drop one here, or{" "}
        <label htmlFor={fileInputId} className="cursor-pointer text-blue-300 hover:underline">
          choose a file
        </label>
        .
      </p>
      <input
        id={fileInputId}
        type="file"
        accept="image/png,image/jpeg,image/webp"
        multiple
        className="sr-only"
        onChange={(event) => {
          void accept(Array.from(event.target.files ?? []));
          event.target.value = "";
        }}
      />

      {error && (
        <p role="alert" className="rounded-lg border border-amber-500/60 bg-amber-950/40 p-3 text-sm text-amber-200">
          {error}
        </p>
      )}

      {attachments.length > 0 && (
        <ul aria-label="Attached images" className="flex flex-wrap gap-3">
          {attachments.map((attachment) => (
            <li
              key={attachment.id}
              className="w-32 rounded-lg border border-dark-border bg-dark-bg p-2"
            >
              <img
                src={attachment.preview}
                alt={attachment.label}
                className="h-20 w-full rounded object-cover"
              />
              <button
                type="button"
                onClick={() =>
                  onAttachmentsChange(attachments.filter((item) => item.id !== attachment.id))
                }
                className="mt-1 w-full rounded text-xs text-dark-muted hover:text-red-300"
              >
                Remove {attachment.label.slice(0, 18)}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
