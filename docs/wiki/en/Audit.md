# Audit log

**Audit log** in the admin interface (`/audit/`, administrators only) lists who did what and when:
the action, the object, the time and the IP address. Filters: action, who, object, dates.
Actions read as names in the interface language ("Issue key"); the code (`keys.issue`) is shown
when you point at the name.

Written now: issuing, disabling, enabling and revoking keys, giving a key to a user, every look at
a key by an administrator and resetting its first view, every look at the SFTP password. Records keep the user and object names,
so they stay readable after the user or the object is deleted. Secrets are never written.

The IP address is the one nginx passes in `X-Forwarded-For`. A forged chain of addresses is not
trusted: the record then shows the proxy address. The header is read only from the proxies in
`LAMNEZIA_TRUSTED_PROXIES` ([Security](Security.md)).
