import type { AttachmentUpload } from "../api/generated";

/**
 * Turning a pasted screenshot into something the API will accept.
 *
 * Kept apart from the widget for the same reason `issueReport.ts` is: two surfaces take
 * prose from a person -- the review panel's feedback box and the Report Issue form --
 * and a second copy of the size and type rules would let them disagree about what a
 * valid paste is.
 *
 * The limits below mirror the server's deliberately. The server's are the ones that
 * matter and are enforced regardless; these exist so an oversized paste fails the
 * instant it happens, while the person still has the box open and their prose in it,
 * rather than after they press submit.
 */

/** Per-image ceiling, matching `MAX_ATTACHMENT_BYTES` in attachments.py. */
export const MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024;

/** What the server will store. Images only: an attachment exists to be looked at. */
export const ACCEPTED_MEDIA_TYPES = ["image/png", "image/jpeg", "image/webp"];

/** An image chosen but not yet submitted. */
export type PendingAttachment = {
  /** Local identity, so removing one does not depend on filename or order. */
  id: string;
  label: string;
  mediaType: string;
  sizeBytes: number;
  dataBase64: string;
  /** data: URL, used for the thumbnail before anything is stored. */
  preview: string;
};

export class AttachmentRejected extends Error {}

function megabytes(bytes: number) {
  return `${Math.round((bytes / (1024 * 1024)) * 10) / 10} MB`;
}

/**
 * Every image in a paste or a drop.
 *
 * A clipboard paste of a screenshot arrives as `clipboardData.items` carrying an image
 * blob directly -- no file picker, which is the interaction this whole feature is
 * about. The same shape covers a drag-and-drop, so both go through here.
 */
export function imagesFromTransfer(transfer: DataTransfer | null): Array<File> {
  if (!transfer) return [];
  const files: Array<File> = [];
  for (const item of Array.from(transfer.items ?? [])) {
    if (item.kind !== "file") continue;
    const file = item.getAsFile();
    if (file && file.type.startsWith("image/")) files.push(file);
  }
  if (files.length === 0) {
    for (const file of Array.from(transfer.files ?? [])) {
      if (file.type.startsWith("image/")) files.push(file);
    }
  }
  return files;
}

/** Read one image into a pending attachment, or reject it with a readable reason. */
export async function readImage(file: File, label?: string): Promise<PendingAttachment> {
  if (!ACCEPTED_MEDIA_TYPES.includes(file.type)) {
    throw new AttachmentRejected(
      `${file.name || "That image"} is ${file.type || "an unknown type"}. AgentJobs stores PNG, JPEG and WebP.`,
    );
  }
  if (file.size > MAX_ATTACHMENT_BYTES) {
    throw new AttachmentRejected(
      `${file.name || "That image"} is ${megabytes(file.size)}, over the ${megabytes(MAX_ATTACHMENT_BYTES)} limit. Crop it or save it smaller, then paste again. Your text is untouched.`,
    );
  }
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new AttachmentRejected("That image could not be read."));
    reader.onload = () => resolve(String(reader.result));
    reader.readAsDataURL(file);
  });
  const comma = dataUrl.indexOf(",");
  if (comma < 0) throw new AttachmentRejected("That image could not be read.");
  return {
    id: crypto.randomUUID(),
    label: label || file.name || "Pasted screenshot",
    mediaType: file.type,
    sizeBytes: file.size,
    dataBase64: dataUrl.slice(comma + 1),
    preview: dataUrl,
  };
}

/** The request shape. `media_type` is absent on purpose: the server reads it from the bytes. */
export function toUploads(pending: Array<PendingAttachment>): Array<AttachmentUpload> {
  return pending.map((item) => ({ data_base64: item.dataBase64, label: item.label }));
}
