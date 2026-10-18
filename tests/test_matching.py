"""Pure matching contracts, conservative ambiguity, and real Hebrew Calculator data."""
from pathlib import Path

import pytest

from mcp_computer_use.agent_cu import Element, parse_elements
from mcp_computer_use.matching import (
    Ambiguous, Match, NoMatch, normalize, rank, resolve, split_role_hint,
)


def elements(*names, role="button"):
    return [Element(f"@e{i}", role, name) for i, name in enumerate(names, 1)]


@pytest.mark.parametrize("source, expected", [
    ("  ＳＡＶＥ　ＡＳ  ", "save as"),
    ("Straße", "strasse"),
    ("שֶׁבַע\u200f", "שבע"),
    ("שמור־בשם", "שמור בשם"),
    ("צה״ל", 'צה"ל'),
    ("ג׳", "ג'"),
    ("Save…", "save"),
    ("Save...", "save"),
    ("Save:  ", "save"),
    ("Save \t as\r\n", "save as"),
    (".", "."),
    ("+", "+"),
    ("=", "="),
    ("×", "×"),
    ("", ""),
])
def test_normalize(source, expected):
    assert normalize(source) == expected
    assert normalize(source) == normalize(expected)


@pytest.mark.parametrize("control", [
    *map(chr, range(0x200B, 0x2010)), "\u061c",
    *map(chr, range(0x202A, 0x202F)),
    *map(chr, range(0x2066, 0x206A)), "\ufeff",
])
def test_normalize_removes_direction_and_zero_width_controls(control):
    assert normalize(f"Sa{control}ve") == "save"


def test_each_tier_and_match_reason():
    cases = [
        ("Save", Element("@e1", "button", "Save"), 1, 1.0, "exact"),
        ("SAVE", Element("@e1", "button", "Save"), 2, 0.95, "normalized"),
        ("7", Element("@e1", "button", "Seven"), 3, 0.9, "synonym"),
        ("SAVE_BUTTON", Element("@e1", "button", "Apply", id="save_button"), 3, 0.9, "id"),
        ("Save", Element("@e1", "button", "Save changes"), 4, 0.6 + 0.3 * 4 / 12, "contains"),
        ("Equls", Element("@e1", "button", "Equals"), 5, 10 / 11, "fuzzy"),
    ]
    for target, element, tier, score, how in cases:
        scored = rank(target, [element])
        assert len(scored) == 1
        assert scored[0].element is element
        assert scored[0].tier == tier
        assert scored[0].score == pytest.approx(score)
        assert scored[0].how == how
        result = resolve(target, [element])
        if tier <= 3:
            assert result == Match(element, how)
        else:
            assert result == Ambiguous([element])


@pytest.mark.parametrize("name, val, target", [
    ("Field", "Some Value", "some value"),
    ("Digit", "Seven", "7"),
    ("Digit", "שבע", "Seven"),
])
def test_value_evidence_is_tier_four(name, val, target):
    element = Element("@e1", "textfield", name, val=val)
    scored = rank(target, [element])
    assert scored[0].tier == 4
    assert scored[0].how == "value"
    assert resolve(target, [element]) == Ambiguous([element])


def test_literal_label_outranks_value():
    value, literal = Element("@e1", "textfield", "Code", val="Save"), Element("@e2", "button", "Save")
    assert resolve("Save", [value, literal]) == Match(literal, "exact")


@pytest.mark.parametrize("target", ["7", "seven", "שבע"])
def test_literal_label_beats_synonyms(target):
    names = [name for name in ("7", "Seven", "שבע") if normalize(name) != normalize(target)]
    synonym_elements = elements(*names)
    literal = Element("@e9", "button", target)
    assert resolve(target, [*synonym_elements, literal]) == Match(literal, "exact")


def test_normalized_label_beats_synonym():
    candidates = elements("שבע", "7", "Seven")
    assert resolve("SEVEN", candidates) == Match(candidates[2], "normalized")


def test_synonym_collision_is_ambiguous():
    candidates = elements("Seven", "שבע")
    # Same tier/score/role: the shorter Hebrew label sorts first.
    assert resolve("7", candidates) == Ambiguous(candidates[::-1])


@pytest.mark.parametrize("target, name", [
    ("Save", "Save changes"),
    ("Save changes", "Save"),
    ("Equls", "Equals"),
    ("eqals", "equals"),
])
def test_single_weak_candidate_is_always_ambiguous(target, name):
    candidates = elements(name)
    assert resolve(target, candidates) == Ambiguous(candidates)


def test_large_score_gap_does_not_promote_contains():
    candidates = elements("Save now", "Save all the many documents")
    scored = rank("Save", candidates)
    assert scored[0].score - scored[1].score >= 0.10
    assert resolve("Save", candidates) == Ambiguous([item.element for item in scored])


@pytest.mark.parametrize("target, roles", [
    ("button Save", {"button"}), ("Save button", {"button"}),
    ("BUTTON Save", {"button"}), ("כפתור שמור", {"button"}),
    ("שמור כפתור", {"button"}), ("link Save", {"link"}),
    ("Save קישור", {"link"}), ("menu item Save", {"menuitem"}),
    ("Save menu item", {"menuitem"}), ("menuitem Save", {"menuitem"}),
    ("תפריט שמור", {"menuitem"}), ("tab Save", {"tab"}),
    ("שמור לשונית", {"tab"}), ("checkbox Save", {"checkbox"}),
    ("Save radio", {"radio"}), ("list item Save", {"listitem"}),
    ("Save list item", {"listitem"}),
])
def test_role_hints(target, roles):
    label, hint = split_role_hint(target)
    assert label in {"Save", "שמור"}
    assert hint == roles


@pytest.mark.parametrize("hint", ["field", "textbox", "edit", "input", "combobox", "שדה"])
def test_text_role_hint(hint):
    label, roles = split_role_hint(f"{hint} Body")
    assert label == "Body"
    assert roles == {"textfield", "textarea", "edit", "combobox", "textbox", "document", "searchfield"}


@pytest.mark.parametrize("target", ["button", "menu item", "Save", "list item"])
def test_standalone_role_does_not_remove_whole_label(target):
    assert split_role_hint(target) == (target, None)


def test_role_hint_preserves_raw_label():
    assert split_role_hint("  button Save  As  ") == ("Save  As", {"button"})


def test_role_hint_disambiguates_and_is_hard_filter():
    link = Element("@e1", "link", "Save")
    button = Element("@e2", "button", "Save")
    assert resolve("button Save", [link, button]) == Match(button, "exact")
    assert resolve("Save link", [link, button]) == Match(link, "exact")
    assert resolve("button Save", [link]) == NoMatch([link])
    assert rank("button Save", [link]) == []
    # An unrelated button must not cause a retry that selects the exact link.
    other = Element("@e3", "button", "Print")
    assert isinstance(resolve("button Save", [link, other]), NoMatch)


def test_explicit_hint_and_caller_filter_are_both_hard():
    link = Element("@e1", "link", "Save")
    button = Element("@e2", "button", "Save")
    assert resolve("link Save", [button, link], role_pref={"button"}) == NoMatch([button, link])
    assert rank("link Save", [button, link], role_pref={"button"}) == []
    assert resolve("link Save", [button, link], role_pref={"link", "button"}) == Match(link, "exact")


def test_caller_role_filter_never_falls_back():
    button = Element("@e1", "button", "Body")
    field = Element("@e2", "document", "Body")
    assert resolve("Body", [button, field], role_pref={"document"}) == Match(field, "exact")
    assert resolve("Body", [button], role_pref={"document"}) == NoMatch([button])
    assert rank("Body", [button], role_pref={"document"}) == []


def test_returned_role_sets_are_not_shared():
    _, hint = split_role_hint("button Save")
    hint.clear()
    assert split_role_hint("button Save")[1] == {"button"}


@pytest.mark.parametrize("target", ["+", "=", ".", "×", "÷", "-", "−"])
def test_symbol_only_exact_match(target):
    literal = Element("@e1", "button", target)
    assert resolve(target, [literal]) == Match(literal, "exact")
    assert resolve(f"  {target}  ", [literal]) == Match(literal, "normalized")


def test_symbol_only_never_uses_contains_or_empty_tokens():
    candidates = elements("Save", "Open", "Equals now", "plus button", "Total + value")
    for target in ("+", "=", ".", "×", "÷"):
        assert rank(target, candidates) == []
        assert isinstance(resolve(target, candidates), NoMatch)


@pytest.mark.parametrize("target, name", [("a", "ab"), ("ab", "abc"), ("xy", "xyz")])
def test_short_targets_never_fuzzy(target, name):
    assert rank(target, elements(name)) == []
    assert isinstance(resolve(target, elements(name)), NoMatch)


def test_hebrew_niqqud_rlm_gershayim_and_maqaf():
    seven = elements("שבע")[0]
    assert resolve("שֶׁבַע\u200f", [seven]) == Match(seven, "normalized")
    quoted = elements('דו"ח שמור')[0]
    assert resolve("דו״ח־שמור", [quoted]) == Match(quoted, "normalized")
    english = elements("Seven")[0]
    assert resolve("שֶׁבַע\u200f", [english]) == Match(english, "synonym")


def test_duplicates_bounded_and_context_preserved():
    candidates = [Element(f"@e{i}", "button", "Close", context=f"Panel {i}") for i in range(10)]
    result = resolve("Close", candidates)
    assert result == Ambiguous(candidates[:5])
    assert [element.context for element in result.candidates] == [f"Panel {i}" for i in range(5)]


def test_normalized_duplicates_remain_ambiguous():
    candidates = elements("Save", "SAVE")
    assert isinstance(resolve("save", candidates), Ambiguous)


def test_rank_order_tier_score_role_length_document():
    candidates = [
        Element("@e1", "link", "Save"),
        Element("@e2", "button", "save"),
        Element("@e3", "button", "Save"),
        Element("@e4", "button", "Apply", id="save"),
        Element("@e5", "button", "Save a very long document"),
        Element("@e6", "link", "Save now"),
    ]
    assert [item.element.ref for item in rank("Save", candidates)] == ["@e3", "@e1", "@e2", "@e4", "@e6", "@e5"]
    # Even role priority cannot make duplicate exact labels auto-resolve.
    assert isinstance(resolve("Save", candidates), Ambiguous)
    roles = ["unknown", "textfield", "radio", "checkbox", "tab", "link", "menuitem", "button"]
    same_name = [Element(f"@e{i}", role, "Save") for i, role in enumerate(roles)]
    assert [item.element.role for item in rank("Save", same_name)] == [
        "button", "menuitem", "link", "tab", "radio", "checkbox", "textfield", "unknown",
    ]
    by_length = [Element("@e1", "button", "An unrelated label", id="apply"),
                 Element("@e2", "button", "OK", id="apply")]
    assert [item.element.ref for item in rank("apply", by_length)] == ["@e2", "@e1"]


def test_no_match_near_sorted_bounded_and_thresholded():
    candidates = elements("axcd", "abyx", "abef", "abgh", "abij", "abkl", "abmn", "zzzzzz")
    result = resolve("abcd", candidates)
    assert isinstance(result, NoMatch)
    assert result.near == candidates[:5]
    assert resolve("zzzz", elements("aaaa", "bbbb")) == NoMatch([])


@pytest.mark.parametrize("target", ["", "  ", "\u200f", ":"])
def test_empty_normalized_targets_never_match(target):
    assert rank(target, [Element("@e1", "button", "", id="", val="")]) == []
    assert resolve(target, elements("Save")) == NoMatch([])


def test_empty_elements():
    assert rank("Save", []) == []
    assert resolve("Save", []) == NoMatch([])


@pytest.fixture
def calculator_he():
    snapshot = (Path(__file__).parent / "fixtures" / "snapshots" / "calculator_he.txt").read_text(encoding="utf-8")
    return parse_elements(snapshot)


@pytest.mark.parametrize("target, ref", [
    ("7", "@e32"), ("+", "@e23"), ("8", "@e33"), ("=", "@e24"),
    ("שבע", "@e32"), ("Seven", "@e32"), ("Equals", "@e24"),
    ("0", "@e25"), ("1", "@e26"), ("2", "@e27"), ("3", "@e28"),
    ("4", "@e29"), ("5", "@e30"), ("6", "@e31"), ("9", "@e34"),
    ("Zero", "@e25"), ("One", "@e26"), ("Two", "@e27"),
    ("Three", "@e28"), ("Four", "@e29"), ("Five", "@e30"),
    ("Six", "@e31"), ("Eight", "@e33"), ("Nine", "@e34"),
    ("Plus", "@e23"), ("-", "@e22"), ("−", "@e22"), ("Minus", "@e22"),
    ("*", "@e21"), ("×", "@e21"), ("Multiply by", "@e21"), ("כפול", "@e21"),
    ("/", "@e20"), ("÷", "@e20"), ("Divide by", "@e20"), ("חלקי", "@e20"),
    (".", "@e35"), ("Decimal separator", "@e35"),
])
def test_real_hebrew_calculator_targets(calculator_he, target, ref):
    result = resolve(target, calculator_he)
    assert isinstance(result, Match)
    assert result.element.ref == ref
    assert result.how == ("exact" if target == result.element.name else "synonym")


@pytest.mark.parametrize("target, label", [
    ("7", "Seven"), ("שבע", "Seven"), ("ועוד", "Plus"),
    ("שווה", "Equals"), ("הכפל ב", "Multiply by"),
    ("חלק ב", "Divide by"), ("תו הפרדה עשרוני", "Decimal separator"),
])
def test_synonyms_resolve_back_to_english(target, label):
    candidates = elements("Seven", "Plus", "Equals", "Multiply by", "Divide by", "Decimal separator")
    result = resolve(target, candidates)
    assert isinstance(result, Match)
    assert result.element.name == label
    assert result.how == "synonym"


@pytest.mark.parametrize("target", ["Seven", "Plus", "Equals", "Multiply by", "Divide by", "Decimal separator"])
def test_english_calculator_literal_labels(target):
    candidates = elements("Seven", "Plus", "Equals", "Multiply by", "Divide by", "Decimal separator")
    result = resolve(target, candidates)
    assert isinstance(result, Match)
    assert result.element.name == target
    assert result.how == "exact"


def test_equls_is_fuzzy_and_ambiguous():
    candidates = parse_elements('[@e1] button "Seven"\n[@e2] button "Equals"')
    result = resolve("Equls", candidates)
    assert result == Ambiguous([candidates[1]])
    assert rank("Equls", candidates)[0].tier == 5


def test_matching_does_not_mutate_inputs():
    candidates = elements("Save", "Save")
    before = [vars(element).copy() for element in candidates]
    roles = {"button"}
    rank("Save", candidates, roles)
    resolve("button Save", candidates, roles)
    assert [vars(element) for element in candidates] == before
    assert roles == {"button"}


@pytest.mark.parametrize("target, other", [
    ("Save", "SAVE"),
    ("Save", "Save\u200f"),
    ("שבע", "שֶׁבַע\u200f"),
    ("ＡＢＣ", "ABC"),
])
def test_raw_match_with_normalized_duplicate_is_ambiguous(target, other):
    candidates = elements(target, other)
    assert rank(target, candidates)[0].tier == 1
    for ordered in (candidates, candidates[::-1]):
        result = resolve(target, ordered)
        assert isinstance(result, Ambiguous)
        assert {element.ref for element in result.candidates} == {"@e1", "@e2"}


def test_role_filter_can_disambiguate_normalized_duplicate():
    button = Element("@e1", "button", "Save")
    link = Element("@e2", "link", "Save\u200f")
    assert resolve("Save", [button, link], role_pref={"button"}) == Match(button, "exact")


def test_id_and_synonym_collision_is_ambiguous():
    synonym = Element("@e1", "button", "Seven")
    ident = Element("@e2", "button", "Other", id="7")
    assert isinstance(resolve("7", [synonym, ident]), Ambiguous)


def test_duplicate_ids_are_ambiguous():
    candidates = [Element("@e1", "button", "First", id="save"),
                  Element("@e2", "button", "Second", id="SAVE")]
    assert isinstance(resolve("save", candidates), Ambiguous)


def test_label_synonym_outranks_live_value():
    value = Element("@e1", "textfield", "Code", val="7")
    label = Element("@e2", "button", "Seven")
    assert resolve("7", [value, label]) == Match(label, "synonym")


@pytest.mark.parametrize("target, val, tier", [
    ("Save", "Save changes", 4),
    ("Equls", "Equals", 5),
])
def test_partial_value_evidence_keeps_value_provenance(target, val, tier):
    field = Element("@e1", "textfield", "Content", val=val)
    scored = rank(target, [field])
    assert scored[0].tier == tier
    assert scored[0].how == "value"
    assert resolve(target, [field]) == Ambiguous([field])


def test_role_filter_no_match_near_uses_all_elements_and_is_bounded():
    buttons = elements(*(["Body"] * 8))
    unrelated = Element("@e9", "document", "Unrelated")
    result = resolve("Body", [*buttons, unrelated], role_pref={"document"})
    assert result == NoMatch(buttons[:5])
    assert resolve("Body", buttons, role_pref=set()) == NoMatch(buttons[:5])


def test_equls_on_hebrew_fixture_is_fuzzy_and_ambiguous(calculator_he):
    result = resolve("Equls", calculator_he)
    assert isinstance(result, Ambiguous)
    assert [element.ref for element in result.candidates] == ["@e24"]
    assert rank("Equls", calculator_he)[0].tier == 5


@pytest.mark.parametrize("target, ref", [
    ("Multiply by number", "@e21"),
    ("Divide by number", "@e20"),
    ("Equals now", "@e24"),
])
def test_contains_on_hebrew_synonym_aliases_is_ambiguous(calculator_he, target, ref):
    result = resolve(target, calculator_he)
    assert isinstance(result, Ambiguous)
    assert ref in [element.ref for element in result.candidates]
    assert next(item for item in rank(target, calculator_he) if item.element.ref == ref).tier == 4


def test_hebrew_filter_no_match_keeps_english_alias_suggestion(calculator_he):
    result = resolve("Equls", calculator_he, role_pref={"document"})
    assert isinstance(result, NoMatch)
    assert result.near[0].ref == "@e24"
    assert len(result.near) <= 5


@pytest.mark.parametrize("target, name, role", [
    ("Save button", "Save", "button"),
    ("Edit profile", "profile", "textfield"),
    ("Check box Enabled", "Enabled", "checkbox"),
])
def test_full_label_and_role_hint_conflict_is_ambiguous(target, name, role):
    literal = Element("@e1", "button", target)
    split = Element("@e2", role, name)
    for candidates in ([literal, split], [split, literal]):
        result = resolve(target, candidates)
        assert isinstance(result, Ambiguous)
        assert {element.ref for element in result.candidates} == {"@e1", "@e2"}
        assert {item.element.ref for item in rank(target, candidates) if item.tier <= 3} == {"@e1", "@e2"}


@pytest.mark.parametrize("target", ["Save button", "Edit profile", "Text field Name", "Check box Enabled"])
def test_lone_literal_role_word_label_is_exact(target):
    literal = Element("@e1", "button", target)
    assert resolve(target, [literal]) == Match(literal, "exact")
    assert rank(target, [literal])[0].tier == 1


def test_full_label_normalized_duplicate_stays_ambiguous():
    candidates = elements("Save button", "Save button\u200f")
    result = resolve("Save button", candidates)
    assert isinstance(result, Ambiguous)
    assert {element.ref for element in result.candidates} == {"@e1", "@e2"}


def test_full_label_normalized_match_beats_weak_split():
    literal = Element("@e1", "link", "Save button\u200f")
    weak = Element("@e2", "button", "Save changes")
    assert resolve("Save button", [weak, literal]) == Match(literal, "normalized")


def test_full_label_id_match_conflicts_with_split_label():
    full = Element("@e1", "button", "Control", id="edit profile")
    split = Element("@e2", "textfield", "profile")
    result = resolve("Edit profile", [full, split])
    assert isinstance(result, Ambiguous)
    assert {element.ref for element in result.candidates} == {"@e1", "@e2"}


def test_full_and_split_can_agree_on_same_element():
    element = Element("@e1", "button", "Save button", id="save")
    assert resolve("Save button", [element]) == Match(element, "exact")
    assert len(rank("Save button", [element])) == 1


def test_full_label_evaluation_keeps_caller_role_filter_hard():
    literal = Element("@e1", "button", "Edit profile")
    field = Element("@e2", "textfield", "profile")
    assert resolve("Edit profile", [literal, field], {"textfield"}) == Match(field, "exact")
    result = resolve("Edit profile", [literal], {"textfield"})
    assert isinstance(result, NoMatch)
    assert result.near[0] is literal


@pytest.mark.parametrize("target, normalized", [
    (":", ":"), ("...", "..."), ("…", "..."),
    ("  :  ", ":"), ("  ...  ", "..."), ("  …  ", "..."),
])
def test_suffix_only_punctuation_keeps_normalized_content(target, normalized):
    assert normalize(target) == normalized
    literal = Element("@e1", "button", target)
    assert resolve(target, [literal]) == Match(literal, "exact")
    normalized_literal = Element("@e2", "button", normalized)
    assert resolve(normalized, [normalized_literal]) == Match(normalized_literal, "exact")


@pytest.mark.parametrize("hint, roles", [
    ("text field", {"textfield", "textarea", "edit", "combobox", "textbox", "document", "searchfield"}),
    ("check box", {"checkbox"}),
])
@pytest.mark.parametrize("leading", [True, False])
def test_spaced_role_hints(hint, roles, leading):
    target = f"{hint} Enabled" if leading else f"Enabled {hint}"
    assert split_role_hint(target) == ("Enabled", roles)
    element = Element("@e1", "textfield" if hint == "text field" else "checkbox", "Enabled")
    assert resolve(target, [element]) == Match(element, "exact")


@pytest.mark.parametrize("hint", ["text field", "check box"])
def test_standalone_spaced_role_is_a_label(hint):
    literal = Element("@e1", "button", hint)
    assert resolve(hint, [literal]) == Match(literal, "exact")


def test_500_element_snapshot_completes_and_caps_result_lists():
    snapshot = 'window "Synthetic"\n' + "\n".join(
        f'  [@e{i}] button "Record {i}" id=record_{i}' for i in range(1, 501)
    )
    candidates = parse_elements(snapshot)
    assert len(candidates) == 500
    exact = resolve("Record 500", candidates)
    assert isinstance(exact, Match)
    assert exact.element.ref == "@e500"
    ambiguous = resolve("Record", candidates)
    assert isinstance(ambiguous, Ambiguous)
    assert len(ambiguous.candidates) == 5
    no_match = resolve("Record", candidates, role_pref={"document"})
    assert isinstance(no_match, NoMatch)
    assert len(no_match.near) == 5
