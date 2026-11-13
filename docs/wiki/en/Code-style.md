# Code style

The code is written for people who will maintain it in a year without knowing the context. The
standard is mandatory for every change; in short:

- logic lives in its layer: Amnezia formats in `amnezia_core/`, SSH in background operations,
  business rules in services, views stay thin;
- a class is created when it keeps state or hides a decision, not for its own sake;
- user interface strings go through gettext (RU and EN);
- secrets and the resources visited by VPN users are never logged;
- behavior or settings changed → `docs/wiki/` (RU and EN) and `CHANGELOG.md` in the same change;
- before a commit: `ruff check`, `ruff format --check`, `mypy`, `pytest`.

The full text is [docs/CODE_STYLE.md](../../CODE_STYLE.md) (in Russian).

<!-- include: docs/CODE_STYLE.md -->
