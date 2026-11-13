# Traffic and stats

How the panel counts traffic and where to see it.

## Counting traffic

Every `LAMNEZIA_TRAFFIC_INTERVAL` seconds the `beat` service reads the peer counters
(`awg show all dump`) from every server with one SSH command, in a background job per server so
that a server that does not answer does not delay the others, and adds the difference to hourly
totals per key. Counters start from zero when a container restarts or a peer is re-created; the
panel notices it — by the counters going down and, if they grew past the old values after the
restart, by the container's start time — and does not lose or double the traffic. On an imported
server the first reading only sets the starting point: traffic from before the panel is not counted.
A key issued or reissued in the panel counts from its first byte, even if it had traffic before
its first reading.
A key is online if its latest handshake is less than 3 minutes old. An XRay key has no handshake:
it is online if its counters grew in a reading within the last `LAMNEZIA_TRAFFIC_INTERVAL` seconds
plus 3 minutes. A key missing from the server (disabled) is not online.

Hourly totals are kept for `LAMNEZIA_TRAFFIC_HOURLY_DAYS` days (35 by default). Once a day older
hours are folded into one row per key and day in the panel time zone; a day in which a plan period
starts is split there. Totals, statistics by day and limits stay exactly the same.

The **Storage** section at the bottom of **Statistics** shows, for the operation log, notification
deliveries, hourly traffic and server monitoring history, the number of rows, the size of the
tables with their indexes (PostgreSQL only) and when the cleanup task last ran with how many rows
it removed. The terms are in [Configuration](Configuration.md).

### XRay

XRay counts traffic per client only with the statistics API turned on, and the Amnezia app does
not turn it on. The panel adds the `stats`, `api` (`127.0.0.1:10085`, inside the container) and
`policy` blocks to `server.json` and gives every client an `email`: a hash of its UUID, so the
UUID itself never shows in the statistics. It does so with every key change on XRay and restarts
the container when the blocks were missing. The app opens such a `server.json` as usual, but
changing the XRay settings in the app writes the file anew without the blocks.

The counters are read with the same SSH command as AmneziaWG (`xray api statsquery`, without
resetting them). XRay starts them from zero on every restart, so the panel reads them just before
it restarts or stops the container itself. A restart from the app (for example, when a key is
added in it) between two readings loses the traffic from the last reading to the restart — at most
one interval; the traffic after the restart is counted in full.

While the API does not answer — after the app rewrote `server.json`, or while the container is
down — the **Statistics** page warns **Traffic is not counted on some servers**: the server and
protocol, when the panel last checked them, the cause and what to do (save the XRay protocol
settings or start the container), with a link to the server. The traffic of those keys is unknown
meanwhile, not zero: it is not counted against the limits and the keys are left out of **Inactive
keys**. Counting resumes from zero once the cause is fixed.

## Statistics page

The **Statistics** page of the admin interface (`/stats/`, administrators and moderators) shows the
traffic of the panel over the last day, week or month. A moderator sees the keys of their zone only.

### Filters

| Filter | Shows |
|---|---|
| Period | Day (by hour), week or month (by day, in the panel time zone). |
| Server | Traffic of every protocol of the server. |

The **Statistics** button on a [user card](Users-plans-limits.md#user-card) and on a key page narrows the page
to that user or key; **Show everyone** removes the narrowing.

### What the page shows

- **Downloaded**, **Uploaded** and **Online now** — totals of the period and keys online (see above);
- a chart of the traffic by hour or by day (downloaded and uploaded as stacked bars). Hovering a
  bar, or tapping it on a phone, shows a tooltip with the date, downloaded, uploaded and the total;
  the exact numbers are also in a table under the chart for screen readers;
- **Top consumers** — ten users who used the most in the period;
- **Limit runs out** — users who, at the pace of their current period, use up their traffic limit
  before the period ends, the soonest first; a limit already used up is marked *reached*;
- **Online now** — keys online now;
- **Inactive keys** — active keys without traffic in the period, the oldest first: candidates to
  revoke. Keys on a server whose statistics are unavailable and of protocols without traffic
  counters (OpenVPN, IPsec) are not listed: no data is not the same as no traffic.

### CSV export

**Export CSV** downloads the traffic of every key in the period and the filters: server, device,
user, status and bytes uploaded and downloaded. A key without data (OpenVPN, IPsec, unavailable
statistics) has empty bytes, not 0. The file opens in Excel and LibreOffice as is.

## Prometheus

Set `LAMNEZIA_METRICS_TOKEN` (for example, `openssl rand -hex 32`) to turn on `/metrics`. Without
the token the endpoint answers 404; a request without the right token gets 403.

```yaml
scrape_configs:
  - job_name: lamnezia
    scheme: https
    authorization:
      credentials: <LAMNEZIA_METRICS_TOKEN>
    static_configs:
      - targets: ["vpn.example.com"]
```

Metrics are per protocol instance (`source` label), never per key:

| Metric | Type | Meaning |
|---|---|---|
| `lamnezia_traffic_received_bytes_total` | counter | Bytes the server received from clients |
| `lamnezia_traffic_sent_bytes_total` | counter | Bytes the server sent to clients |
| `lamnezia_peers` | gauge | Peers in the latest reading (disabled keys are not counted) |
| `lamnezia_peers_online` | gauge | Of them online: a handshake in 3 minutes or, for XRay, traffic growth |
| `lamnezia_traffic_collected_timestamp_seconds` | gauge | Time of the latest reading; falls behind when the server cannot be read |

Alert example: `time() - lamnezia_traffic_collected_timestamp_seconds > 900`.
