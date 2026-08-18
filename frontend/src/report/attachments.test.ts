import { describe, expect, it } from "vitest";

import {
  AttachmentRejected,
  imagesFromTransfer,
  MAX_ATTACHMENT_BYTES,
  readImage,
  toUploads,
} from "./attachments";

function imageFile(name: string, type: string, size = 12) {
  return new File([new Uint8Array(size)], name, { type });
}

/** The shape a clipboard or a drop hands over: `items` first, `files` as the fallback. */
function transfer(files: Array<File>, { itemsOnly = true } = {}): DataTransfer {
  return {
    items: itemsOnly
      ? files.map((file) => ({ kind: "file", getAsFile: () => file }))
      : [],
    files: itemsOnly ? [] : files,
  } as unknown as DataTransfer;
}

describe("imagesFromTransfer", () => {
  it("reads image blobs out of a paste", () => {
    const png = imageFile("screenshot.png", "image/png");
    expect(imagesFromTransfer(transfer([png]))).toEqual([png]);
  });

  it("falls back to files when a drop exposes no items", () => {
    const png = imageFile("dropped.png", "image/png");
    expect(imagesFromTransfer(transfer([png], { itemsOnly: false }))).toEqual([png]);
  });

  it("ignores a paste that carries no image, so pasting text still pastes text", () => {
    expect(imagesFromTransfer(transfer([imageFile("notes.txt", "text/plain")]))).toEqual([]);
    expect(imagesFromTransfer(null)).toEqual([]);
  });
});

describe("readImage", () => {
  it("turns an image into a base64 payload with a preview", async () => {
    const attachment = await readImage(imageFile("shot.png", "image/png"));
    expect(attachment.mediaType).toBe("image/png");
    expect(attachment.label).toBe("shot.png");
    expect(attachment.dataBase64.length).toBeGreaterThan(0);
    expect(attachment.preview.startsWith("data:image/png;base64,")).toBe(true);
    expect(toUploads([attachment])).toEqual([
      { data_base64: attachment.dataBase64, label: "shot.png" },
    ]);
  });

  it("refuses a type the server would not store", async () => {
    await expect(readImage(imageFile("notes.pdf", "application/pdf"))).rejects.toBeInstanceOf(
      AttachmentRejected,
    );
  });

  it("refuses an oversized image and says the text is safe", async () => {
    // Mirrors the server's ceiling so the rejection happens while the box is still
    // open, rather than after submit.
    const huge = imageFile("huge.png", "image/png", MAX_ATTACHMENT_BYTES + 1);
    await expect(readImage(huge)).rejects.toThrow(/Your text is untouched/);
  });
});
