# Installation guide

AgentJobs requires Python 3.11 or newer. It is not yet published to PyPI, so install it
from a clone today:

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install
npm --prefix frontend ci && npm --prefix frontend run build
poetry run agentjobs open
```

**The `npm` line is not optional from a clone.** The React bundle is a build artifact:
it is gitignored, no `poetry install` produces it, and without it `/app/` is the only
part of AgentJobs that does not work. Installing a release wheel is the case that needs
no Node — see the contract below.

`agentjobs open` starts the local server if necessary and opens the packaged React
application at `/app/`. It checks before it opens anything: if nothing answers, if the
port belongs to another service, or if the bundle was never built, it says so and exits
non-zero rather than handing a browser a URL that cannot work. Use `poetry run agentjobs
serve` when you want the server to remain attached to the current terminal; it prints
the same warning at startup and serves the REST API and MCP tools regardless.

## Release-wheel contract

A published release wheel contains the production React bundle, manifest, icons, and
service worker. Installing and running that wheel requires Python only—no Node, npm, or
frontend build step:

```bash
pip install agentjobs
agentjobs open
```

The `pip install` example applies once a release is published. Contributors need Node
only to develop or build the frontend, not to use AgentJobs.

## Contributor setup

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
python scripts/bootstrap.py
poetry run python scripts/check.py
```

`scripts/bootstrap.py` is the supported setup for **any** fresh checkout, a clone or a
git worktree. It runs `poetry install`, `npm ci` and `playwright install chromium`, and
then verifies that the environment imports *this* checkout's source rather than a
neighbouring one's — the check that distinguishes a green suite from a green suite that
tested somebody else's code. Do not substitute a hand-run `poetry install` plus `npm
install`: `npm install` can rewrite `package-lock.json`, and neither does the
provenance check.

The bootstrap does not build the frontend bundle, because `scripts/check.py` builds one
as its `build` stage. Run the `npm --prefix frontend run build` line above if you want
`/app/` before you have run the gate.

The complete check is ten named stages — formatting, lint, types, the generated API
document and client, the generated PWA icons, frontend lint, the Python suite, the
jsdom component tests, the production build, and the Playwright end-to-end tests
against a live server. See [ENGINEERING.md](https://github.com/jeffposey/agentjobs/blob/main/ENGINEERING.md)
for the stage table and what each one costs. Release
artifacts must be created with `poetry run python scripts/build_release.py`; it produces
and verifies a platform-independent `py3-none-any` wheel, then boots the installed
server with Node removed from `PATH`.
