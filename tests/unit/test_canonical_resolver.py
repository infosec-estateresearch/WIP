"""CanonicalResolver tests — three deterministic paths + the not-found case."""

from collections import defaultdict

from src.wip_app.sync.canonical_resolver import CanonicalResolver


def _resolver(*, display_map=None, normalised_map=None):
    r = CanonicalResolver()
    for name, ids in (display_map or {}).items():
        for i in ids:
            r.by_display[name].add(i)
    for name, ids in (normalised_map or {}).items():
        for i in ids:
            r.by_normalised[name].add(i)
    return r


def test_path_a_canonical_name_exact_match_resolves():
    r = _resolver(display_map={"Hugh James": [42]})
    cc_id, status = r.resolve("Hugh James", "hugh james")
    assert (cc_id, status) == (42, "resolved")


def test_path_b_alias_normalised_match_resolves():
    r = _resolver(normalised_map={"wbw": [99]})
    cc_id, status = r.resolve("WBW Solicitors", "wbw")
    assert (cc_id, status) == (99, "resolved")


def test_no_match_returns_unresolved():
    r = _resolver(display_map={"Other": [1]})
    cc_id, status = r.resolve("Chrous Law", "chrous law")
    assert (cc_id, status) == (None, "unresolved")


def test_ambiguous_two_different_cc_ids_returns_ambiguous():
    """Display says one, alias says another → operator must decide."""
    r = _resolver(display_map={"X": [5]}, normalised_map={"x": [6]})
    cc_id, status = r.resolve("X", "x")
    assert cc_id is None and status == "ambiguous"


def test_same_id_from_both_paths_is_resolved_not_ambiguous():
    """If display AND alias both point at the same cc_id, that's resolved."""
    r = _resolver(display_map={"Y": [7]}, normalised_map={"y": [7]})
    cc_id, status = r.resolve("Y", "y")
    assert (cc_id, status) == (7, "resolved")


def test_empty_inputs_return_unresolved():
    r = _resolver(display_map={"": [1]})
    cc_id, status = r.resolve(None, None)
    assert cc_id is None and status == "unresolved"
