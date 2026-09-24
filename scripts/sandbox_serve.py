"""Serve a review sandbox on loopback, and on the tailnet too whenever Tailscale is up.

Every ``*_sandbox.py`` beside this file ends by serving the application, and until
task-567 each one chose its own address. Most chose ``127.0.0.1``, so whether the owner
could open a review from a phone depended on which older sandbox a new script had been
copied from: task-562 handed over a loopback URL that no other device can reach.
``tests/test_sandbox_serve.py`` now fails any sandbox that calls uvicorn itself.

**Tailscale is optional, and used whenever it is there.** Nothing here requires it: with
no Tailscale, or Tailscale logged out, a sandbox serves loopback exactly as before and
says why. ``AGENTJOBS_SANDBOX_TAILNET=0`` turns it off on a machine that has it.

**On the tailnet, looking works and writing is refused -- by design.** A sandbox is not
behind the tailnet front door (``scripts/tailscale-service-host``), the component that
proves who a remote caller is, so ``principals.py`` refuses every write from the tailnet
with ``no_proven_identity``. Layout, colours and tap targets are reviewable from the
phone; a button that writes is pressed on the loopback URL. Nor is plain HTTP to a
tailnet address a secure context, so a feature that needs one (dictation) still wants
the ``tailscale serve`` HTTPS proxy its own sandbox documents.

Two uvicorn servers over one application and one store, rather than binding ``0.0.0.0``:
the bind surface stays exactly the two addresses named, so a laptop on a cafe network does
not also answer on its Wi-Fi address because somebody wanted to look at a page on a phone.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, NamedTuple, Optional

__all__ = ["Tailnet", "TAILNET_ENV", "review_base", "serve", "tailnet"]

#: ``0`` / ``false`` / ``no`` / ``off`` serves loopback only on a machine with Tailscale.
TAILNET_ENV = "AGENTJOBS_SANDBOX_TAILNET"

LOOPBACK = "127.0.0.1"

_CANDIDATES = (Path("C:/Program Files/Tailscale/tailscale.exe"), Path("tailscale"))


class Tailnet(NamedTuple):
    """This machine on the tailnet: the address to bind and the name to hand out."""

    address: str
    #: The MagicDNS name, e.g. ``host.example.ts.net``, or the address when there is none.
    name: str


def _tailscale(*arguments: str) -> Optional[str]:
    for candidate in _CANDIDATES:
        try:
            result = subprocess.run(
                [str(candidate), *arguments],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
    return None


def _disabled() -> bool:
    return os.environ.get(TAILNET_ENV, "").strip().lower() in {"0", "false", "no", "off"}


def tailnet() -> Optional[Tailnet]:
    """This machine's tailnet IPv4 and name, or ``None`` when Tailscale is not up.

    Asked of the CLI rather than read from an interface list, because that is the one
    answer that is wrong for the right reason when Tailscale is installed but logged
    out -- an address on the adapter that no peer can route to.
    """
    if _disabled():
        return None
    output = _tailscale("ip", "-4")
    if output is None:
        return None
    address = output.strip().splitlines()[0].strip()
    name = address
    status = _tailscale("status", "--json")
    if status is not None:
        try:
            dns = str(json.loads(status).get("Self", {}).get("DNSName") or "").rstrip(".")
        except (ValueError, AttributeError):
            dns = ""
        if dns:
            name = dns
    return Tailnet(address=address, name=name)


def review_base(port: int, remote: Optional[Tailnet] = None) -> str:
    """The base URL to put in a review request: the tailnet name when there is one."""
    remote = remote if remote is not None else tailnet()
    return f"http://{remote.name if remote else LOOPBACK}:{port}"


def serve(app: Any, *, port: int, host: str = LOOPBACK, **config: Any) -> None:
    """Serve ``app`` on ``host`` and, when Tailscale is up, on this machine's tailnet address.

    Blocks until Ctrl-C. ``config`` is passed to ``uvicorn.Config`` (``lifespan="off"``,
    for instance). The loopback server runs in a daemon thread and the tailnet one on the
    main thread, so Ctrl-C reaches the one that owns the terminal and the process exits
    with it.
    """
    import uvicorn

    config.setdefault("log_level", "warning")
    remote = tailnet()

    def server(bind: str) -> uvicorn.Server:
        return uvicorn.Server(uvicorn.Config(app, host=bind, port=port, **config))

    local = f"http://{host}:{port}"
    if remote is None or remote.address == host:
        if remote is None:
            why = (
                f"{TAILNET_ENV} is off"
                if _disabled()
                else "Tailscale is not running or not logged in"
            )
            print(f"[sandbox] {why}: serving {local} only", flush=True)
        else:
            print(f"[sandbox] serving {review_base(port, remote)} only", flush=True)
        server(host).run()
        return

    phone = review_base(port, remote)
    print(f"[sandbox] phone/tablet: {phone}/app/  (looking works; writes are refused)", flush=True)
    print(f"[sandbox] this machine: {local}/app/  (writes work here)", flush=True)
    print(
        f"[sandbox] put the phone URL in the review request: any {local} link above "
        f"works from the phone as {phone}",
        flush=True,
    )

    loopback = server(host)
    thread = threading.Thread(target=loopback.run, daemon=True)
    thread.start()
    try:
        server(remote.address).run()
    finally:
        loopback.should_exit = True
