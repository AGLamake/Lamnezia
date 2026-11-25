# Panel move

**System → Panel move** moves the panel with all its data to another machine: the panel installs
itself there, restores its [archive](Backup.md#panel-archive) and checks the copy. The only manual
step is pointing the panel domain to the new machine.

## Before the move

- Add the new machine as a server (**Servers → Add**): the move connects to it over SSH as root or
  a user with passwordless sudo. A plain machine is fine, and so is a VPN server or a member of a
  group with a relay: Amnezia containers and the relay keep working.
- The machine runs Debian or Ubuntu, has at least 3 GB free on `/`, about 1 GB of memory with
  swap (`MemTotal` + `SwapTotal` at least 900 MB) and has no panel yet (`/opt/lamnezia` and the
  nginx site `lamnezia` must not exist).
- It is not the machine this panel runs on: the form refuses a server whose address is the panel
  domain, the address the panel is opened at, or an IP address these names point to. The
  preparation checks it again over SSH and stops before any change when the panel connects from
  an address of the machine (one of its own or of its containers) or the panel domain points there.
- With nginx installed, the panel adds its site to it. Without nginx, the bundled Caddy needs TCP
  80/443 and UDP 443 free; a relay never takes them.
- Servers that allow SSH only from the panel address must allow the address of the new machine.

## How it goes

1. Choose the machine and start the move. The *prepare* operation first checks the machine over SSH
   (state *checking the new machine*): the system, free disk, memory, ports, that it is not the
   panel itself. Meanwhile the panel still accepts changes. A machine that does not fit fails the
   move with the reason and a hint on the move page; the panel never becomes read-only.
2. Once the machine fits, the panel is **read-only**: pages open, but changes answer `503` with
   `Retry-After`; background jobs and bots pause. The live updates of the support pages (WebSocket)
   are closed: the pages refresh by polling instead. Operations queued a moment before end with "the panel is moving"; a running operation stops the move — start it
   again when the operation ends.
3. The preparation makes an archive with a one-time password, uploads the same code version and
   the archive, runs `install.sh --restore … --moving` and checks that the
   copy answers. The archive password reaches the installer through stdin only; the archive is
   deleted on both machines afterwards. The copy starts read-only too. The installer runs on the
   new machine apart from the SSH session (`systemd-run`, or `setsid` without systemd) and writes
   its output to a log the panel reads every few seconds, so a dropped connection does not kill it.
   A panel of a release passes `--image X.Y.Z`: the new machine pulls the ready image of the same
   version from the registry and builds it only when the pull fails. A panel built between releases
   has the image built there; a panel set to its own `LAMNEZIA_IMAGE` passes it in its archive.
4. A relay on the machine is checked before the install and after it: if a Docker reinstall stopped
   it, it is started again; it must run with the same ports.
5. When the copy is ready, press **Confirm the move**. The copy starts accepting changes; this panel
   stays read-only for good.
6. Point the panel domain to the new machine. With nginx, the certificate request is retried every
   10 minutes (`lamnezia-certificate.timer`) until the domain points there; Caddy retries by itself.
7. When the domain points to the new machine, the old panel stops itself (see
   [Automatic stop of the old copy](#automatic-stop-of-the-old-copy)). With the option off, stop it
   by hand: `cd /opt/lamnezia && docker compose down`.

## Move progress screen

Starting, confirming or cancelling a move opens the **move progress** screen (also the *Progress*
link on the move page). It lists the steps of every operation of the move with their status and
reloads itself while an operation runs.

After the confirmation the screen shows a **Panel domain** block: the time since the hand-over and
where the A record of the domain from `SITE_URL` points. The domain is looked up through several
resolvers, `LAMNEZIA_DOMAIN_RESOLVERS` (by default the system one, `1.1.1.1` and `8.8.8.8`), and
the screen lists what each one answers and when it was checked: the A records, *no A record* or
*unreachable*. A resolver that does not answer within two seconds is marked *unreachable* and does
not count. The status:

- *not checked yet* — the background check has not run yet;
- *has no A record* — no resolver that answers gives an IPv4 address for the domain;
- *points to another machine* — at least one resolver gives an A record that leads elsewhere (for
  example, still to the old machine, while its cache lasts) or gives no A record;
- *points to the new machine* — every resolver that answers leads, with every A record, to the
  new machine's address; the old panel
  stops itself, or, with the automatic stop off, the screen suggests stopping it once DNS caches
  have expired.

A background job checks the domain once a minute (on the old panel it runs while the panel is
read-only too); the page itself never looks up DNS. The checks stop once the domain has switched.
A panel opened by an IP address (no domain in `SITE_URL`) has nothing to check.

When the domain switches, the old copy sends the *Panel domain switched* notification
(`panel_domain_switched`) once per move, with a link to the move progress screen, to the channels
subscribed to it in [Notifications](Notifications.md). The read-only old panel still delivers
notifications.

## Automatic stop of the old copy

The option **Stop this copy once the domain points to the new machine** is on by default in the
move form. Until the stop is requested it can be turned off or on again on the move progress
screen, also while the panel is read-only.

The old copy stops only when all of this holds:

- the move is confirmed (the old panel is in the *moved away* state);
- the option is on;
- every resolver that answers leads, with every A record of the panel domain, to the new machine;
- the copy on the new machine answers its health check and accepts changes.

Then the *Move the panel: stop the old copy* operation appears in the operations journal. It checks
the new copy over SSH and leaves the request file `stop-after-move` in the panel archives folder;
the `lamnezia-stop.timer` on the host of the old panel finds it within a minute, removes it and
runs `docker compose down`. If the new copy does not answer, the operation fails and the old copy
keeps running; retry the operation from the journal once the new copy is up.

The panel has no access to Docker, so the timer is installed by `install.sh`. A panel installed by
an older `install.sh` has no timer: run `install.sh` once more on its host, or stop the old copy by
hand. If the stop request waits longer than five minutes, the move progress screen warns about it
and shows the command `cd /opt/lamnezia && docker compose down`; the warning goes once the host
takes the request.

## Cancel and failures

- **Cancel the move** (before the confirmation, or after a failure) removes the copy from the new
  machine and lets this panel accept changes again. Only a copy made by the move is removed (it is
  marked by `/opt/lamnezia/.moving`).
- A failed preparation removes what it installed. If the new machine cannot be reached, the cancel
  still unfreezes the panel and says how to remove the copy by hand.
- **An interrupted preparation** (the worker or the whole stack restarted halfway) fails by
  itself: the preparation reports that it is alive while it runs, and if it stays silent for 15
  minutes the panel fails the move and accepts changes again. Then cancel the move or start it once
  more; the copy left on the new machine is removed first.
- While the preparation, the confirmation or the cancel runs, the host does not update the panel:
  the panel keeps the flag `request/moving` in the folder of the update unit, and
  `lamnezia-deploy.sh` refuses to deploy (exit code 75) or to apply an update requested from the
  panel. Deploy again once the move step is over; a flag left by a stopped worker counts only until
  its time runs out.
- A copy that already accepts changes is never removed; confirm the move again instead.
- **A confirmation that did not finish** (the new machine went down halfway) leaves the panel
  read-only: the copy may already accept changes. The screen then offers **Retry the confirmation**
  for when the new machine answers again, and **Cancel without the new machine** for when it
  cannot be reached. The latter runs nothing there: this panel accepts changes again, and the copy
  stays as it is. Do not point the domain to it and remove it by hand
  (`cd /opt/lamnezia && docker compose down -v`, then delete `/opt/lamnezia` and the nginx site
  `lamnezia`, if there is one); until then there may be two panels.
- Changes made in the old panel during the move do not reach the new one, which is why the old
  panel is frozen for the whole time.
