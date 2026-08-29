"""Hard-block all network access in this process, so "offline-safe" can be measured.

``docs/competition_specification.md`` says official judging may run with network access
disabled. Claiming the agent is fine with that is cheap; *proving* it is what this does.
Installing the guard makes any outbound network operation raise
``NetworkAccessAttempted`` immediately, so a run that completes under it did so without
touching the network -- a far stronger statement than "we did not write any HTTP calls".

Two independent layers, because either alone has a hole:

* **An audit hook** (``sys.addaudithook``) catches the CPython-level events -- it fires
  even for a C extension dialling out, and cannot be dodged by importing ``socket`` under
  a different name. It also cannot be uninstalled, which is why every caller here is a
  dedicated process (a test runner or ``offline_check.py``), never the agent itself.
* **Monkeypatched entry points**, so the failure names what was attempted instead of
  surfacing as an opaque audit error, and so socket *creation* is blocked and not merely
  socket *use*.

The agent never imports any of this. Enforcement belongs to the harness; shipping a
process-wide audit hook inside a library the organizer imports would be rude at best.
"""

from __future__ import annotations

import socket
import sys


class NetworkAccessAttempted(RuntimeError):
    """Raised the instant anything in this process reaches for the network."""


# CPython audit events that mean "outbound network", not merely "a socket object
# exists". `socket.connect` and the two resolver events are the ones no real network call
# can avoid. urllib/ftplib/smtplib are listed so the error names the library involved.
BLOCKED_EVENTS = (
    "socket.connect",
    "socket.getaddrinfo",
    "socket.gethostbyname",
    "socket.sendto",
    "urllib.Request",
    "ftplib.connect",
    "smtplib.connect",
    "smtplib.send",
)

# Captured before anything is patched, so the blocking subclass below still inherits the
# genuine socket type and `ssl` can subclass it as usual.
_REAL_SOCKET = socket.socket

_installed = False


def _audit_hook(event: str, args: tuple) -> None:
    if event in BLOCKED_EVENTS:
        raise NetworkAccessAttempted(f"network access attempted: {event} {args!r}")


def _blocked(name: str):
    def refuse(*args: object, **kwargs: object):
        raise NetworkAccessAttempted(f"network access attempted: {name}")

    return refuse


def install() -> None:
    """Make this process incapable of network access. Idempotent; not reversible."""
    global _installed
    if _installed:
        return
    _installed = True

    sys.addaudithook(_audit_hook)

    # Socket *construction*, so a caller cannot even get as far as connect(). This has to
    # be a subclass, not a function: `ssl.py` does `class SSLSocket(socket)` at import
    # time, and rebinding `socket.socket` to a plain function makes importing ssl -- and
    # therefore asyncio, joblib, and all of scikit-learn -- fail with an unrelated
    # TypeError. Blocking the constructor while staying a class keeps that chain
    # importable and still refuses every socket this process might open.
    class _BlockedSocket(_REAL_SOCKET):  # type: ignore[misc, valid-type]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise NetworkAccessAttempted("network access attempted: socket.socket()")

    socket.socket = _BlockedSocket  # type: ignore[assignment]
    socket.create_connection = _blocked("socket.create_connection")  # type: ignore[assignment]
    socket.getaddrinfo = _blocked("socket.getaddrinfo")  # type: ignore[assignment]
    socket.gethostbyname = _blocked("socket.gethostbyname")  # type: ignore[assignment]

    # urllib is imported lazily so the guard itself does not drag in the network stack
    # when it is the very thing under test.
    try:
        import urllib.request

        urllib.request.urlopen = _blocked("urllib.request.urlopen")  # type: ignore[assignment]
    except Exception:  # noqa: BLE001 - a Python without urllib is already offline enough
        pass


def is_installed() -> bool:
    return _installed
