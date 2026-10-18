"""Pure UI-label ranking; only unique exact, normalized or synonym/ID matches resolve."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .agent_cu import Element


@dataclass
class Scored:
    element: Element
    tier: int
    score: float
    how: str = "exact"


@dataclass
class Match:
    element: Element
    how: str


@dataclass
class Ambiguous:
    candidates: list[Element]


@dataclass
class NoMatch:
    near: list[Element]


def normalize(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.translate(str.maketrans({"\u05be": " ", "\u05f3": "'", "\u05f4": '"'}))
    s = re.sub(r"[\u0591-\u05c7]", "", s)
    s = re.sub(r"[\u200b-\u200f\u061c\u202a-\u202e\u2066-\u2069\ufeff]", "", s)
    stripped = re.sub(r"(?:…|\.\.\.|:)\s*$", "", s)
    if stripped.strip():
        s = stripped
    return " ".join(s.split()).casefold()


_TEXT_ROLES = {"textfield", "textarea", "edit", "combobox", "textbox", "document", "searchfield"}
_ROLE_HINTS = {
    "button": {"button"}, "כפתור": {"button"},
    "link": {"link"}, "קישור": {"link"},
    "menu item": {"menuitem"}, "menuitem": {"menuitem"}, "תפריט": {"menuitem"},
    "tab": {"tab"}, "לשונית": {"tab"},
    "checkbox": {"checkbox"}, "check box": {"checkbox"}, "radio": {"radio"},
    "list item": {"listitem"}, "listitem": {"listitem"},
    **{hint: _TEXT_ROLES for hint in ("field", "text field", "textfield", "textbox", "edit", "input", "combobox", "שדה")},
}


def split_role_hint(target: str) -> tuple[str, set[str] | None]:
    """Remove one leading/trailing role phrase, preserving the label's case and spacing."""
    tokens = list(re.finditer(r"\S+", target))
    for count in (2, 1):
        if len(tokens) <= count:
            continue
        leading = " ".join(normalize(token[0]) for token in tokens[:count])
        trailing = " ".join(normalize(token[0]) for token in tokens[-count:])
        if leading in _ROLE_HINTS:
            return target[tokens[count].start():].strip(), set(_ROLE_HINTS[leading])
        if trailing in _ROLE_HINTS:
            return target[:tokens[-count].start()].strip(), set(_ROLE_HINTS[trailing])
    return target, None


_SYNONYM_GROUPS = (
    ("0", "zero", "אפס"),
    ("1", "one", "אחת", "אחד"),
    ("2", "two", "שתיים", "שניים"),
    ("3", "three", "שלוש"),
    ("4", "four", "ארבע"),
    ("5", "five", "חמש"),
    ("6", "six", "שש"),
    ("7", "seven", "שבע"),
    ("8", "eight", "שמונה"),
    ("9", "nine", "תשע"),
    ("+", "plus", "ועוד"),
    ("-", "−", "minus", "פחות"),
    ("*", "×", "multiply", "multiply by", "הכפל ב", "כפול"),
    ("/", "÷", "divide", "divide by", "חלק ב", "חלקי"),
    ("=", "equals", "שווה"),
    (".", "decimal separator", "תו הפרדה עשרוני"),
)
_SYNONYMS = {}
for _group in _SYNONYM_GROUPS:
    _aliases = frozenset(normalize(alias) for alias in _group)
    for _alias in _aliases:
        _SYNONYMS[_alias] = _aliases

_ROLE_PRIORITY = {
    "button": 0, "menuitem": 1, "link": 2, "tab": 3,
    "checkbox": 4, "radio": 4, **{role: 5 for role in _TEXT_ROLES},
}


def _ratio(target: str, value: str) -> float:
    return SequenceMatcher(None, target, value).ratio() if value else 0.0


def _values(element: Element) -> list[tuple[str, bool]]:
    """Search labels and their language aliases; retain the live-value provenance."""
    name, ident, val = (normalize(value) for value in (element.name, element.id, element.val))
    values = [(name, False), (ident, False)]
    values.extend((alias, False) for alias in sorted(_SYNONYMS.get(name, ())))
    values.append((val, True))
    values.extend((alias, True) for alias in sorted(_SYNONYMS.get(val, ())))
    return [(value, is_value) for value, is_value in values if value]


def _score(target: str, nt: str, element: Element) -> Scored | None:
    name = normalize(element.name)
    ident = normalize(element.id)
    val = normalize(element.val)
    if element.name == target:
        return Scored(element, 1, 1.0, "exact")
    if name == nt:
        return Scored(element, 2, 0.95, "normalized")
    aliases = _SYNONYMS.get(nt, frozenset())
    if name in aliases:
        return Scored(element, 3, 0.9, "synonym")
    if ident == nt:
        return Scored(element, 3, 0.9, "id")
    # Live content is weak evidence even when it equals the target or a synonym.
    if val == nt or val in aliases:
        return Scored(element, 4, 0.9, "value")
    values = _values(element)
    tokens = set(re.findall(r"\w+", nt))
    contains = []
    if tokens:
        for value, is_value in values:
            value_tokens = set(re.findall(r"\w+", value))
            if value_tokens and (tokens <= value_tokens or value_tokens <= tokens):
                score = 0.6 + 0.3 * min(len(nt), len(value)) / max(len(nt), len(value))
                contains.append((score, "value" if is_value else "contains"))
    if contains:
        score, how = max(contains, key=lambda item: item[0])
        return Scored(element, 4, score, how)
    if len(nt) >= 3:
        similarity, how = max(((_ratio(nt, value), "value" if is_value else "fuzzy")
                               for value, is_value in values),
                              key=lambda item: item[0], default=(0.0, "fuzzy"))
        if similarity >= 0.80:
            return Scored(element, 5, similarity, how)
    return None


def _roles(hint: set[str] | None, role_pref: set[str] | None) -> set[str] | None:
    if hint is not None and role_pref is not None:
        return hint & role_pref
    return hint if hint is not None else role_pref


def _eligible(elements: list[Element], roles: set[str] | None) -> list[Element]:
    return [element for element in elements if roles is None or element.role in roles]


def _rank(target: str, elements: list[Element]) -> list[Scored]:
    nt = normalize(target)
    if not nt:
        return []
    scored = [match for element in elements if (match := _score(target, nt, element)) is not None]
    return _sort_scores(nt, scored)


def _sort_scores(nt: str, scored: list[Scored]) -> list[Scored]:
    # Python's stable sort preserves document order after the specified ties.
    return sorted(scored, key=lambda match: (
        match.tier, -match.score, _ROLE_PRIORITY.get(match.element.role, 6),
        abs(len(normalize(match.element.name)) - len(nt)),
    ))



def _interpret(target: str, elements: list[Element], role_pref: set[str] | None) -> tuple[str, list[Scored], bool]:
    """Preserve full labels; role words are an alternate interpretation, not a rewrite."""
    full = _rank(target, _eligible(elements, role_pref))
    label, hint = split_role_hint(target)
    if hint is None:
        return target, full, False
    split = _rank(label, _eligible(elements, _roles(hint, role_pref)))
    full_strong = [item for item in full if item.tier <= 3]
    split_strong = [item for item in split if item.tier <= 3]
    if full_strong:
        full_refs = {item.element.ref for item in full_strong}
        split_refs = {item.element.ref for item in split_strong}
        if split_strong and full_refs != split_refs:
            # Both readings identify strong, different candidates: never choose
            # one interpretation just because its raw spelling ranks first.
            combined = {item.element.ref: item for item in full_strong}
            for item in split_strong:
                combined.setdefault(item.element.ref, item)
            return target, _sort_scores(normalize(target), list(combined.values())), True
        return target, full, False
    return label, split, False


def rank(target: str, elements: list[Element], role_pref: set[str] | None = None) -> list[Scored]:
    return _interpret(target, elements, role_pref)[1]


def resolve(target: str, elements: list[Element], role_pref: set[str] | None = None) -> Match | Ambiguous | NoMatch:
    label, scored, conflicting_interpretations = _interpret(target, elements, role_pref)

    if scored:
        best = scored[0]
        # Raw bytes may rank first, but visually identical normalized labels
        # share one ambiguity group across tiers 1 and 2.
        if best.tier <= 2:
            unique = sum(normalize(item.element.name) == normalize(label) for item in scored) == 1
        else:
            unique = sum(item.tier == best.tier for item in scored) == 1
        if best.tier <= 3 and unique and not conflicting_interpretations:
            return Match(best.element, best.how)
        return Ambiguous([item.element for item in scored[:5]])
    nt = normalize(label)
    if not nt:
        return NoMatch([])
    # Suggestions explain a failed filter without relaxing it for matching.
    near = [(element, max((_ratio(nt, value) for value, _ in _values(element)), default=0.0))
            for element in elements]
    near = sorted((item for item in near if item[1] >= 0.4), key=lambda item: -item[1])
    return NoMatch([element for element, _ in near[:5]])
