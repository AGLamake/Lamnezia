# Архитектура

Панель — Django-приложение, которое запускает docker compose:

- `web` обслуживает HTTP и WebSocket (ASGI: Django + Channels);
- `worker` выполняет фоновые задачи Celery: все SSH-операции на серверах, уведомления;
- `beat` запускает периодические задачи: трафик, мониторинг, сверку с серверами, копии файлов
  серверов;
- `postgres` хранит данные, `redis` — брокер задач, слой каналов WebSocket и кэш.

Панель подключается к VPN-серверам по SSH и правит те же файлы, что и приложение Amnezia
(`awg0.conf`, `clientsTable`). Всё знание о форматах Amnezia — в `amnezia_core/` (чистый Python
без Django и сети); SSH выполняется только в фоновых операциях (`apps/operations`), никогда в
веб-запросе.

Исходник схем ниже — [docs/ARCHITECTURE.md](../../ARCHITECTURE.md); причины решений —
в [docs/DESIGN.md](../../DESIGN.md).

<!-- include: docs/ARCHITECTURE.md -->
