# AgentJobs React frontend

This is a separate Vite project; the repository-root `package.json` remains dedicated
to schema-documentation tooling.

```powershell
npm install
npm run dev
npm run check
```

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

oxlint is the linter: it is a single fast binary with no plugin-resolution tree, so
`npm run check` can run it on every change. Vitest is intentionally not installed by
this scaffold; task 085 owns the test harness and its dependencies.

Tailwind is bundled through its Vite plugin. The stylesheet uses Tailwind 4's
CSS-first theme variables and a `.dark` custom variant, preserving the Jinja UI's
palette and class-driven dark mode without a CDN or `tailwind.config.js`.
