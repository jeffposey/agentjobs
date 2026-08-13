# AgentJobs React frontend

This is a separate Vite project; the repository-root `package.json` remains dedicated
to schema-documentation tooling.

```powershell
npm install
npm run dev
npm run check
```

`npm run check` is the frontend gate. It first exports FastAPI's OpenAPI document
directly from the application (no running server required), regenerates the checked-in
client, and fails when either `openapi.json` or `src/api/generated/` is stale. To
intentionally refresh both artifacts, run `npm run generate:api` and review the diff.

The development server runs at `http://localhost:5173/app/` and proxies `/api` to the
AgentJobs server at `http://127.0.0.1:8765`. `npm run build` writes the production
bundle to `frontend/dist/`; FastAPI serves that bundle at `/app`.

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

oxlint is the linter: it is a single fast binary with no plugin-resolution tree, so
`npm run check` can run it on every change. Vitest is intentionally not installed by
this scaffold; task 085 owns the test harness and its dependencies.

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
