from dataclasses import dataclass
from difflib import SequenceMatcher
import unicodedata


def normalized(value: str) -> str:
    # Only outer whitespace/NFC/case are normalized. Internal spaces are preserved.
    return unicodedata.normalize("NFC", value).strip().casefold()


def spacing_key(value: str) -> str:
    # A secondary identity key only. Exact names keep priority, and collisions
    # are choices rather than dictionary overwrites. Never join source lines.
    return ''.join(c for c in normalized(value) if not c.isspace() or c in '\r\n')


@dataclass(frozen=True)
class Match:
    canonical: str | None
    status: str
    suggestions: tuple[str, ...] = ()


class ItemCatalog:
    def __init__(self, items: list[str], aliases: dict[str, str]):
        self.items = {normalized(item): item for item in items}
        if len(self.items) != len(items) or any(not normalized(item) for item in items):
            raise ValueError("Ambiguous or blank Database names need explicit resolution")
        self.aliases = {normalized(alias): target for alias, target in aliases.items()}
        if len(self.aliases) != len(aliases):
            raise ValueError("Ambiguous aliases")
        for alias, target in self.aliases.items():
            if target not in self.items.values() or not alias:
                raise ValueError("Aliases must refer to an exact known Database item")
            if alias in self.items and self.items[alias] != target:
                raise ValueError("An alias cannot override a Database item")

    def match(self, value: str) -> Match:
        key = normalized(value)
        if key in self.items:
            return Match(self.items[key], "exact")
        if key in self.aliases:
            target = self.aliases[key]
            if self.items.get(normalized(target)) == target:
                return Match(target, "alias")
            # A removed/renamed master item invalidates an earlier alias.
            return Match(None, "unknown")
        scores = sorted(
            ((SequenceMatcher(None, key, name).ratio(), item) for name, item in self.items.items()),
            reverse=True,
        )
        suggestions = tuple(item for score, item in scores[:3] if score >= 0.65)
        return Match(None, "possible_typo" if suggestions else "unknown", suggestions)

    def identity(self, value: str) -> Match:
        """Resolve only established identity rules, without fuzzy suggestions.

        Reads and writes share exact/alias/unique-spacing identity. A possible
        typo is never sufficient to classify a stored expense.
        """
        key = normalized(value)
        if key in self.items:
            return Match(self.items[key], 'exact')
        if key in self.aliases:
            target = self.aliases[key]
            if self.items.get(normalized(target)) == target:
                return Match(target, 'alias')
        names = tuple(item for item in self.items.values() if spacing_key(item) == spacing_key(value))
        if len(names) == 1:
            return Match(names[0], 'spacing')
        if names:
            return Match(None, 'ambiguous_spacing', names)
        return Match(None, 'unknown')

    def resolve(self, value: str) -> Match:
        identity = self.identity(value)
        return self.match(value) if identity.status == 'unknown' else identity
