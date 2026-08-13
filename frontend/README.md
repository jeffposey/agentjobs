# AgentJobs React frontend

This is a separate Vite project; the repository-root `package.json` remains dedicated
to schema-documentation tooling.

```powershell
npm install
npm run install:e2e
npm run dev
npm run check
```

`npm run install:e2e` provisions Playwright's pinned Chromium build once per machine
or whenever Playwright is upgraded. `npm run check` is the frontend gate. It first exports FastAPI's OpenAPI document
directly from the application (no running server required), regenerates the checked-in
client, fails when either `openapi.json` or `src/api/generated/` is stale, runs the
jsdom component suite, builds the app, and runs one Playwright path against a real
server and a fresh temporary project. The full repository gate is
`poetry run python scripts/check.py` from the repository root; that is the command to
run before commit because it includes both pytest and this frontend check. To
intentionally refresh generated artifacts, run `npm run generate:api` and review the
diff.

`npm run check` also verifies that the committed PWA icons match
`assets/app-icon.svg`. Run `npm run generate:icons` after changing that source. The
production build injects the current hashed JavaScript and CSS into a shell-only
service worker; task API responses are deliberately never cached. Cross-device HTTPS,
installation, teardown, and offline behavior are documented in
[`docs/mobile-access.md`](../docs/mobile-access.md).

The development server runs at `http://localhost:5173/app/` and proxies `/api` to the
AgentJobs server at `http://127.0.0.1:8765`. `npm run build` writes the production
bundle directly to `src/agentjobs/frontend_dist/`, the package-data location FastAPI
serves at `/app` both in a checkout and from an installed wheel. The directory is
generated and gitignored.

## Release packaging

From the repository root, build release artifacts with:

```powershell
poetry run python scripts/build_release.py
```

The release script runs `npm ci` and `npm run build` before invoking Poetry, so the
documented release path cannot silently package an old bundle. It then opens the wheel
and verifies the HTML shell, hashed JavaScript and CSS, manifest, service worker, and
all required icons. Building a release therefore needs Node; installing and running
the resulting universal wheel does not. The sdist carries the same already-built
bundle, so building a wheel from it also needs only Python. Do not publish artifacts
made with raw `poetry build`; the release script is the freshness and content gate. As
its final check, the script installs the wheel into an isolated target, removes Node
from `PATH`, starts `agentjobs serve`, and requests the shell and PWA assets.

## Pinned toolchain

| Package | Installed version |
| --- | --- |
| React / React DOM | 19.2.8 |
| React Router DOM | 7.18.2 |
| Vite | 8.2.1 |
| TypeScript | 6.0.3 |
| `@vitejs/plugin-react` | 6.0.5 |
| Tailwind CSS / `@tailwindcss/vite` | 4.3.3 |
| oxlint | 1.78.0 |
| `@hey-api/openapi-ts` | 0.97.3 |
| TanStack Query | 5.101.4 |
| MSW | 2.15.0 |
| Playwright | 1.62.1 |
| Vitest | 4.1.10 |
| React Testing Library / DOM Testing Library | 16.3.2 / 10.4.1 |
| `@testing-library/jest-dom` | 7.0.1 |
| jsdom | 30.0.1 |

oxlint is the linter: it is a single fast binary with no plugin-resolution tree, so
`npm run check` can run it on every change.

Vitest and React Testing Library run in jsdom. Checked-in, human-readable API examples
live under `src/test/fixtures/`, one JSON file per response shape. Tests should assert
the rendered value a person reads or acts on; the first example verifies the exact task
count, including its value, rather than merely checking that a paragraph exists.
Tests that exercise API-backed pages add handlers to the shared MSW server in
`src/test/api-mock.ts`. Unexpected requests fail, and the generated client remains in
place so request serialization and response parsing are part of the test. Playwright is
deliberately limited to one high-value path: browser task creation through a real
server and temporary project.

Tailwind is bundled through its Vite plugin. The stylesheet uses Tailwind 4's
CSS-first theme variables and a `.dark` custom variant, preserving the Jinja UI's
palette and class-driven dark mode without a CDN or `tailwind.config.js`.

## Generated API client

Hey API was selected because one generator emits the closed schema types, Fetch-based
SDK, and TanStack Query v5 options while supporting this project's TypeScript 6
toolchain. `openapi-typescript` was evaluated, but its current peer dependency only
accepts TypeScript 5. The application imports request and response contracts only from
`src/api/generated/`; generated files are never edited by hand.

The `js-yaml` 4.3.1 override keeps the generator's transitive YAML parser on its
patched release. Regeneration is deliberately explicit rather than an install or build
hook, so installing or building the frontend never depends on a live AgentJobs server.

The same routers serve unscoped and project-scoped API paths. FastAPI cannot infer a
path parameter used only by a dependency reading the request, so the scoped router
mount declares `project_id` as a dependency. This backend declaration does not change
runtime routing; it makes the existing path contract complete in OpenAPI so generated
clients can substitute the project identifier.
