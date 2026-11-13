"""Hooks of protocol changes for other parts of the panel.

Sent after the transaction of the change commits, for notifications:

- `keys_changed(user, server)`: the protocol settings of `server` changed; the user's keys must
  be imported into the app again.
- `keys_revoked(user, server)`: the protocol is removed from `server` with its clients; the user's
  keys on it are revoked.
- `keys_moved(user, server)`: the user's keys of a legacy AmneziaWG on `server` got new ones on
  AmneziaWG 3.1; they must be imported into the app before the old container goes.

Sent in the operation, under the server lock:

- `container_removing(instance, host, ctx)`: the container of `instance` is about to be removed
  and its keys are kept for a restore. A receiver that raises keeps the container.

Sent in the operation, once the panel has saved what it read back:

- `service_changed(service)`: a new secret or new settings of `service` are on its server.
"""

from django.dispatch import Signal

keys_changed = Signal()
keys_revoked = Signal()
keys_moved = Signal()
container_removing = Signal()
service_changed = Signal()
