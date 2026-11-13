"""Every action code written to the journal from the code reads as a name, not as the code."""

import ast
from pathlib import Path

from django.conf import settings

from apps.audit.services.actions import action_label

APPS = Path(settings.BASE_DIR) / "apps"


class _Module:
    """String constants of a module and the names it imports from other modules of the apps."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.tree = ast.parse(path.read_text(encoding="utf-8"))
        self.constants = {
            target.id: node.value.value
            for node in self.tree.body
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
            for target in node.targets
            if isinstance(target, ast.Name) and isinstance(node.value.value, str)
        }
        self.imports = {
            alias.asname or alias.name: (node.module, alias.name)
            for node in self.tree.body
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("apps.")
            for alias in node.names
        }

    def audit_codes(self) -> list[ast.expr]:
        """First arguments of `audit.log(...)` calls."""
        return [
            node.args[0]
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "log"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "audit"
            and node.args
        ]


class _Sources:
    def __init__(self) -> None:
        self._modules = {
            _dotted(path): _Module(path) for path in APPS.rglob("*.py") if "tests" not in path.parts
        }

    def codes(self) -> dict[str, str]:
        """Each code the journal gets from a literal or a module constant, and where it is."""
        found = {}
        for name, module in self._modules.items():
            for argument in module.audit_codes():
                code = self._resolve(module, argument)
                if code is not None:
                    found[code] = f"{name}:{argument.lineno}"
        return found

    def _resolve(self, module: _Module, argument: ast.expr) -> str | None:
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            return argument.value
        if not isinstance(argument, ast.Name):
            return None
        if argument.id in module.constants:
            return module.constants[argument.id]
        origin, name = module.imports.get(argument.id, ("", ""))
        source = self._modules.get(origin)
        return source.constants.get(name) if source else None


def _dotted(path: Path) -> str:
    parts = path.relative_to(APPS.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def test_the_scan_finds_codes_from_literals_and_constants() -> None:
    codes = _Sources().codes()

    assert {"bots.command", "protocols.readdress", "keys.resolve_drift"} <= set(codes)


def test_every_code_has_a_name_or_is_an_operation() -> None:
    unnamed = {
        code: where for code, where in _Sources().codes().items() if action_label(code) == code
    }

    assert unnamed == {}
