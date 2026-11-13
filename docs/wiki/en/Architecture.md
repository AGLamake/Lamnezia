# Architecture

The panel is a Django application started by docker compose:

- `web` serves HTTP and WebSocket (ASGI: Django + Channels);
- `worker` runs background jobs with Celery: every SSH operation on a server, notifications;
- `beat` schedules the periodic jobs: traffic, monitoring, comparison with the servers, copies of
  the server files;
- `postgres` keeps the data, `redis` is the job broker, the WebSocket channel layer and the cache.

The panel reaches the VPN servers over SSH and edits the same files as the Amnezia app
(`awg0.conf`, `clientsTable`). All knowledge of the Amnezia formats lives in `amnezia_core/` (plain
Python without Django or network); SSH runs only in background operations (`apps/operations`), never
in a web request.

The diagrams below are kept in [docs/ARCHITECTURE.md](../../ARCHITECTURE.md) (in Russian); the
reasons behind the decisions are in [docs/DESIGN.md](../../DESIGN.md).

<!-- include: docs/ARCHITECTURE.md -->
