# Installation guide

AgentJobs requires Python 3.11 or newer. It is not yet published to PyPI, so install it
from a clone today:

```bash
git clone https://github.com/jeffposey/agentjobs.git
cd agentjobs
poetry install
poetry run agentjobs open
```

`agentjobs open` starts the local server if necessary and opens the packaged React
application at `/app/`. Use `poetry run agentjobs serve` when you want the server to
remain attached to the current terminal.

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
poetry install
npm --prefix frontend install
npm --prefix frontend run install:e2e
poetry run python scripts/check.py
```

The complete check runs Python tests, generated API and icon checks, lint, React
component tests, the production build, and one real-server Playwright path. Release
artifacts must be created with `poetry run python scripts/build_release.py`; it produces
and verifies a platform-independent `py3-none-any` wheel, then boots the installed
server with Node removed from `PATH`.
