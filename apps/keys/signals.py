"""Signals of key operations that other apps act on."""

from django.dispatch import Signal

# Sent before the panel restarts or stops an XRay container, which zeroes its traffic counters.
# Arguments: `source` (the instance) and `host` (RemoteHost, under the server lock).
xray_restarting = Signal()

# Sent before a key operation adds new clients to an instance: their counters start from zero.
# Arguments: `source` (the instance) and `public_keys` (of the new keys).
clients_adding = Signal()

# Sent after a key operation changed the files of an instance on its server.
# Arguments: `source` (the instance).
keys_written = Signal()

# Sent when the panel finds a new shortId in the server.json of an XRay instance and has put it
# into the stored profiles of its keys. Arguments: `source` (the instance) and `short_id`.
xray_short_id_changed = Signal()
