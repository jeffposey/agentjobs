import { spawnSync } from "node:child_process";

function runGit(args, encoding) {
  const result = spawnSync("git", args, { encoding });
  if (result.error) {
    console.error(`Unable to inspect generated API files: ${result.error.message}`);
    process.exit(1);
  }
  return result;
}

const generatedPath = "src/api/generated";
const diff = runGit(["diff", "--quiet", "--", generatedPath]);
const untracked = runGit(
  ["ls-files", "--others", "--exclude-standard", "--", generatedPath],
  "utf8",
);

if (diff.status > 1) {
  process.exit(diff.status);
}

if (untracked.status !== 0) {
  process.stderr.write(untracked.stderr);
  process.exit(untracked.status ?? 1);
}

if (diff.status === 1 || untracked.stdout.trim()) {
  console.error(
    "Generated API client files are stale. Run `npm run generate:api` and commit the result.",
  );
  process.exit(1);
}

console.log("Generated API client files are current.");
