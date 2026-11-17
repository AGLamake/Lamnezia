# Panel move

**System → Panel move** moves the panel with all its data to another machine: the panel installs
itself there, restores its [archive](Backup.md#panel-archive) and checks the copy. The only manual
step is pointing the panel domain to the new machine.

## Before the move

- Add the new machine as a server (**Servers → Add**): the move connects to it over SSH as root or
  a user with passwordless sudo. A plain machine is fine, and so is a VPN server or a member of a
  group with a relay: Amnezia containers and the relay keep working.
- The machine runs Debian or Ubuntu, has at least 3 GB free on `/` and has no panel yet
  (`/opt/lamnezia` and the nginx site `lamnezia` must not exist).
- It is not the machine this panel runs on: the form refuses a server whose address is the panel
  domain, the address the panel is opened at, or an IP address these names point to. The
  preparation checks it again over SSH and stops before any change when the panel connects from
  an address of the machine (one of its own or of its containers) or the panel domain points there.
- With nginx installed, the panel adds its site to it. Without nginx, the bundled Caddy needs TCP
  80/443 and UDP 443 free; a relay never takes them.
- Servers that allow SSH only from the panel address must allow the address of the new machine.

## How it goes

1. Choose the machine and start the move. From this click the panel is **read-only**: pages open,
   but changes answer `503` with `Retry-After`; background jobs and bots pause. Operations queued a
   moment before end with "the panel is moving"; a running operation stops the move — start it
   again when the operation ends.
2. The *prepare* operation checks the machine, makes an archive with a one-time password, uploads
   the same code version and the archive, runs `install.sh --restore … --moving` and checks that the
   copy answers. The archive password reaches the installer through stdin only; the archive is
   deleted on both machines afterwards. The copy starts read-only too.
3. A relay on the machine is checked before the install and after it: if a Docker reinstall stopped
   it, it is started again; it must run with the same ports.
4. When the copy is ready, press **Confirm the move**. The copy starts accepting changes; this panel
   stays read-only for good.
5. Point the panel domain to the new machine. With nginx, the certificate request is retried every
   10 minutes (`lamnezia-certificate.timer`) until the domain points there; Caddy retries by itself.
6. When the domain points to the new machine, stop the old panel: `cd /opt/lamnezia && docker
   compose down`.

## Move progress screen

Starting, confirming or cancelling a move opens the **move progress** screen (also the *Progress*
link on the move page). It lists the steps of every operation of the move with their status and
reloads itself while an operation runs.

After the confirmation the screen shows a **Panel domain** block: the time since the hand-over and
where the A record of the domain from `SITE_URL` points:

- *not checked yet* — the background check has not run yet;
- *has no A record* — the domain does not resolve to an IPv4 address;
- *points to another machine* — at least one A record leads elsewhere (for example, still to the
  old machine);
- *points to the new machine* — every A record leads to the new machine's address; on the old
  panel the screen suggests stopping it once DNS caches have expired.

A background job checks the domain once a minute (on the old panel it runs while the panel is
read-only too); the page itself never looks up DNS. The checks stop once the domain has switched.
A panel opened by an IP address (no domain in `SITE_URL`) has nothing to check.

## Cancel and failures

- **Cancel the move** (before the confirmation, or after a failure) removes the copy from the new
  machine and lets this panel accept changes again. Only a copy made by the move is removed (it is
  marked by `/opt/lamnezia/.moving`).
- A failed preparation removes what it installed. If the new machine cannot be reached, the cancel
  still unfreezes the panel and says how to remove the copy by hand.
- A copy that already accepts changes is never removed; confirm the move again instead.
- Changes made in the old panel during the move do not reach the new one, which is why the old
  panel is frozen for the whole time.
