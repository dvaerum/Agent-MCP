"""A real AF_UNIX peer for tests that model a Unix-socket-fronted deploy.

``rate_limit.is_trusted_peer`` resolves UDS-peer trust from ``SO_PEERCRED``
— the kernel's record of who opened the connection. That fact only exists
on a genuinely connected socket, so a ``Mock`` transport cannot stand in
for it: a test that wants "the peer is our co-located reverse proxy" has
to hand over a real socket.

``socket.socketpair`` is the way to get one without a listener: two
connected AF_UNIX endpoints in THIS process, whose peer credentials are
therefore this process's own pid/uid — exactly the "proxy running as the
router's user, on the same host" shape those tests mean by "UDS peer".

One pair for the whole session, deliberately never closed: the credentials
live on the open fd, every caller wants the same trusted peer, and the fds
are reclaimed at interpreter exit.

The mismatch direction (a UDS peer running as SOMEONE ELSE) is covered in
``test_so_peercred_peer_trust.py`` by moving ``os.getuid``, since a test
process cannot conjure a peer it doesn't own.
"""

from __future__ import annotations

import socket

_PAIR: tuple[socket.socket, socket.socket] | None = None


def uds_peer_socket() -> socket.socket:
    """A live AF_UNIX endpoint whose ``SO_PEERCRED`` peer is this process."""
    global _PAIR
    if _PAIR is None:
        _PAIR = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    return _PAIR[0]
