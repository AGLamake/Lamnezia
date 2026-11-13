"""Lossless reader and writer for WireGuard-style configs (`[Section]` + `Key = Value` lines)."""

import re

from amnezia_core.errors import ConfigFormatError

_HEADER = re.compile(r"^\s*\[(?P<name>[^\]]+)\]\s*$")
_ENTRY = re.compile(r"^\s*(?P<key>[A-Za-z0-9_]+)\s*=(?P<value>.*)$")
_KEY = re.compile(r"[A-Za-z0-9_]+")
_FORBIDDEN_IN_VALUE = ("\n", "\r", "#")


def _parse_entry(line: str) -> tuple[str, str] | None:
    match = _ENTRY.match(line)
    if match is None:
        return None
    # wg-quick drops everything after '#'; base64 keys and AWG I1-I5 never contain it.
    value = match["value"].split("#", 1)[0].strip()
    return match["key"], value


def _entry_line(key: str, value: str) -> str:
    # A newline or '#' in a value would inject or cut config lines.
    if not _KEY.fullmatch(key) or any(char in value for char in _FORBIDDEN_IN_VALUE):
        raise ConfigFormatError(f"Unsafe config entry for key {key!r}.")
    return f"{key} = {value}"


class Section:
    """One `[Name]` block. Keeps its raw lines, so comments and unknown lines survive edits."""

    def __init__(self, lines: list[str]) -> None:
        header = _HEADER.match(lines[0]) if lines else None
        if header is None:
            raise ConfigFormatError("A config section must start with a [Name] header.")
        self.name = header["name"].strip()
        self.lines = lines

    @classmethod
    def build(cls, name: str, entries: list[tuple[str, str]]) -> "Section":
        """New section in the layout the Amnezia app writes: header, entries, blank line."""
        return cls([f"[{name}]", *(_entry_line(key, value) for key, value in entries), ""])

    def entries(self) -> list[tuple[str, str]]:
        """All `Key = Value` pairs in file order."""
        return [entry for line in self.lines[1:] if (entry := _parse_entry(line)) is not None]

    def get(self, key: str) -> str | None:
        """First value for the key; keys are case-insensitive as in wg-quick."""
        return next((v for k, v in self.entries() if k.lower() == key.lower()), None)

    def get_all(self, key: str) -> list[str]:
        return [v for k, v in self.entries() if k.lower() == key.lower()]

    def set(self, key: str, value: str) -> None:
        """Replace the first line with this key or add one after the last entry."""
        line = _entry_line(key, value)
        index = self._index_of(key)
        if index is not None:
            self.lines[index] = line
            return
        entry_indexes = [i for i, line in enumerate(self.lines) if _parse_entry(line)]
        self.lines.insert((entry_indexes[-1] if entry_indexes else 0) + 1, line)

    def remove(self, key: str) -> None:
        self.lines = [
            line
            for line in self.lines
            if (entry := _parse_entry(line)) is None or entry[0].lower() != key.lower()
        ]

    def _index_of(self, key: str) -> int | None:
        for index, line in enumerate(self.lines):
            entry = _parse_entry(line)
            if entry is not None and entry[0].lower() == key.lower():
                return index
        return None


class WgConfig:
    """Whole config file. `parse(text).serialize()` returns the text unchanged."""

    def __init__(
        self, preamble: list[str], sections: list[Section], trailing_newline: bool
    ) -> None:
        self.preamble = preamble
        self.sections = sections
        self.trailing_newline = trailing_newline

    @classmethod
    def parse(cls, text: str) -> "WgConfig":
        trailing_newline = text.endswith("\n")
        lines = text.split("\n")
        if trailing_newline:
            lines.pop()
        preamble: list[str] = []
        sections: list[Section] = []
        for line in lines:
            if _HEADER.match(line):
                sections.append(Section([line]))
            elif sections:
                sections[-1].lines.append(line)
            else:
                preamble.append(line)
        return cls(preamble, sections, trailing_newline)

    def serialize(self) -> str:
        lines = [*self.preamble, *(line for section in self.sections for line in section.lines)]
        text = "\n".join(lines)
        return text + "\n" if self.trailing_newline and lines else text

    def sections_named(self, name: str) -> list[Section]:
        return [section for section in self.sections if section.name.lower() == name.lower()]

    def append(self, section: Section) -> None:
        # The new section must start on its own line even if the file had no final newline.
        self.trailing_newline = True
        self.sections.append(section)

    def remove(self, section: Section) -> None:
        self.sections.remove(section)
