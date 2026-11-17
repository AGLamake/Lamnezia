# Ports

Some mobile and home providers block or throttle UDP on ports above 9999. The Amnezia app gives
AmneziaWG the port `55424`, so on such a network the VPN may not connect at all or runs much slower
than over Wi-Fi of another provider. A port up to 9999 usually passes as ordinary traffic.

## How the panel picks a port

When the port field of the install form is left empty:

- **AmneziaWG 2.0** and **AmneziaWG (legacy)** get the first UDP port that is free on the server,
  starting from `5542` and going up to `9999`, then from `1024` to `5541`. A port is taken when
  another Amnezia container or the reachability probe uses it. So the second AmneziaWG on a server
  gets the next port, for example `5542` and `5543`. If every one of them is taken, the port of the
  app (`55424`) is used.
- The other protocols keep the port of the Amnezia app (`51820/udp` for WireGuard, `443/tcp` for
  XRay, `1194/udp` for OpenVPN and so on). For WireGuard, whose `51820` is above 9999, the form
  says so under an empty port field and offers the first free port up to 9999 to enter instead.

The suggested port is shown in the table of the install form. Protocols already installed keep their
port: changing it breaks every client until it imports the key again, so it is done only on purpose
in **Settings**.

The install form and the AmneziaWG settings form show a warning as soon as the port is above 9999.
The warning does not block saving: a high port is fine where the provider passes it.

## Speed test by port

To check a particular provider, compare a low and a high port on the same server. **Speed test by
port** is a button in the **Protocols and services** section of the server card and a link under
the port field of the install and settings forms (administrators only).

1. Choose **how to measure**: **In the browser** (the default) or **iperf3 on a device**.
2. Enter up to four ports separated by commas. The form suggests one free port up to 9999 and one
   above it. Ports taken by Amnezia containers are refused, and for iperf3 the SSH port too.
3. Choose for how many minutes the test runs (30 by default, at most 180) and press **Start**.
4. The panel builds a small image on the server and starts the `lamnezia-iperf` container. It runs
   without root and without logs, and removes itself when the time is up. **Stop now** removes it
   at once.
5. If the hosting provider has its own firewall, open these ports there: UDP for the browser, TCP
   and UDP for iperf3.

### In the browser

The container (the `lamnezia-webtransport` image, Python with `aioquic`) listens on the ports over
UDP as a WebTransport server. That is HTTP/3, the same UDP an AmneziaWG client uses. It makes a
temporary certificate itself, and the private key never leaves the container; the panel learns
only the certificate fingerprint and passes it to the page. Strangers cannot connect: the address
holds a random key only the test page knows.

Open the test page on a device in the network you want to check (a phone over mobile data, a
laptop connected to the phone), **with the VPN off**, and press **Measure**. For every port the page
downloads for 8 seconds, then uploads for 8 seconds and shows the speed in Mbit/s. The result stays
on the page; nothing is sent to the panel.

- The browser needs WebTransport: Chrome or Edge 100+, Firefox 125+. In Safari and older browsers
  the page suggests iperf3.
- A server at an IPv6 address cannot be measured in the browser; choose iperf3.
- "No answer" means the browser could not connect over UDP to this port: most likely UDP is
  blocked there (or the hosting firewall keeps the port closed).
- The test server is written in Python and gives about 100–200 Mbit/s on one core. That is enough
  to compare ports; to push a fast channel to its limit use iperf3.

### With iperf3

On a device in the network you want to check (a phone over mobile data with Termux, a laptop
connected to the phone), **with the VPN off**, install `iperf3` and run for every port the commands
shown on the page (the container on the `lamnezia-iperf3` image listens on the ports for TCP and
UDP):

```sh
iperf3 -c vpn.example.com -p 5542 -u -b 200M -t 10 -R   # download over UDP
iperf3 -c vpn.example.com -p 5542 -u -b 200M -t 10      # upload over UDP
iperf3 -c vpn.example.com -p 5542 -t 10 -R              # download over TCP
```

Then repeat them with the high port. Only one client can test a port at a time; if `iperf3` says the
server is busy, wait for the previous run to end.

### Reading the results

- In the browser compare the download and upload between the ports: if one port is much slower or
  shows "no answer" instead of a speed, the provider throttles or blocks UDP on that port.
- For UDP look at the receiver line at the end: the bitrate and **Lost/Total Datagrams**. A loss
  of a few percent at `-b 200M` is normal when the channel is slower than 200 Mbit/s; compare the
  ports with each other, not with zero.
- If one port gives much lower bitrate or much higher loss over UDP while TCP on both ports is
  about the same, the provider throttles UDP on that port. Use a port that behaves like TCP.
- If the client cannot connect over UDP at all (no datagrams received) but TCP works, UDP on that
  port is blocked.
- If all ports and TCP are slow alike, the limit is the channel or the server, not the port.
  Repeat the test at another time of day before drawing a conclusion.

### Recording the results

Write down the provider, the connection type and the numbers, so the next server is set up right
away:

| Provider, connection | Port | UDP down, Mbit/s / loss | UDP up, Mbit/s / loss | TCP down, Mbit/s |
|---|---|---|---|---|
| … | 5542 | … | … | … |
| … | 55424 | … | … | … |

Results differ between providers, regions and even times of day, so the panel suggests a port up to
9999 for every new AmneziaWG: it passes in more networks, and a high port can still be chosen by hand.
