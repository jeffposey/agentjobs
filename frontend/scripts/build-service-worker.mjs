import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dist = resolve(frontendRoot, "dist");
const indexHtml = await readFile(resolve(dist, "index.html"), "utf8");
const template = await readFile(resolve(frontendRoot, "src", "service-worker.js"), "utf8");
const builtAssets = [...indexHtml.matchAll(/(?:src|href)="(\/app\/assets\/[^"]+)"/g)]
  .map((match) => match[1]);
const shellUrls = [
  "/app/",
  "/app/manifest.webmanifest",
  "/app/icons/icon-192.png",
  "/app/icons/icon-512.png",
  "/app/icons/icon-maskable-512.png",
  ...builtAssets,
];
const revision = createHash("sha256").update(template).update(shellUrls.join("\n")).digest("hex").slice(0, 12);
const serviceWorker = template
  .replace("__CACHE_NAME__", `agentjobs-shell-${revision}`)
  .replace("__SHELL_URLS__", JSON.stringify(shellUrls, null, 2));

await writeFile(resolve(dist, "sw.js"), serviceWorker, "utf8");
console.log(`Wrote dist/sw.js with ${shellUrls.length} shell resources (${revision}).`);
