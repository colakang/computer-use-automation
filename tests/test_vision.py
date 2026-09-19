"""VisionSurface matching logic against a canned screen parse (no model, no browser)."""

from cua import store
from cua.schema import Target
from cua.surface.vision import build_elements, matches

DETAIL = {
    "elements": [
        {"role": "link", "text": "Member Inquiry", "bbox": [10, 70, 120, 85]},
        {"role": "heading", "text": "Member Detail", "bbox": [190, 66, 330, 86]},
        {"role": "link", "text": "Open New Share", "bbox": [186, 402, 296, 418], "obscured": True},
    ],
    "tables": [
        {"header": None, "rows": [
            [{"text": "Member Number:", "bbox": [0, 0, 1, 1]}, {"text": "10388", "bbox": [0, 0, 1, 1]}],
            [{"text": "Name:", "bbox": [0, 0, 1, 1]}, {"text": "Marcus Oyelaran", "bbox": [340, 136, 466, 164]}],
        ]},
        {"header": ["Share ID", "Type", "Description", "Balance", "Available"], "rows": [
            [{"text": "S0009", "bbox": [0, 0, 1, 1]}, {"text": "Share Draft", "bbox": [0, 0, 1, 1]},
             {"text": "Everyday Checking", "bbox": [0, 0, 1, 1]}, {"text": "$640.11", "bbox": [800, 300, 950, 320]},
             {"text": "$640.11", "bbox": [0, 0, 1, 1]}],
            [{"text": "S0002", "bbox": [0, 0, 1, 1]}, {"text": "Share Savings", "bbox": [0, 0, 1, 1]},
             {"text": "Regular Savings", "bbox": [0, 0, 1, 1]}, {"text": "$88,120.40", "bbox": [800, 330, 950, 350]},
             {"text": "$88,120.40", "bbox": [0, 0, 1, 1]}],
        ]},
    ],
}


def resolve(target: Target, els):
    for s in target.strategies:
        hits = [e for e in els if matches(e, s)]
        if len(hits) == 1:
            return hits[0], s.by
    return None, None


def test_dom_recorded_targets_resolve_on_a_pixel_parse():
    cap = store.load("member.savings_balance.read")
    els = build_elements(DETAIL)
    bal, by = resolve(cap.steps[7].target, els)     # 'Balance' of row Type='Share Savings'
    assert by == "table_cell" and bal.name == "$88,120.40", "second row: found by meaning, not position"
    name, by = resolve(cap.steps[8].target, els)    # value cell of row 'Name:'
    assert by == "table_cell" and name.name == "Marcus Oyelaran"
    nav, by = resolve(cap.steps[3].target, els)
    assert by == "role_name" and nav.center == (65.0, 77.5)


def test_structural_strategies_never_match_on_pixels():
    cap = store.load("member.savings_balance.read")
    els = build_elements(DETAIL)
    for s in cap.steps[7].target.strategies:
        if s.by == "css":
            assert not any(matches(e, s) for e in els)


def test_caption_normalisation_and_obscured_flag():
    els = build_elements({"elements": [
        {"role": "textbox", "text": "", "caption": "Search Value", "bbox": [290, 140, 400, 160]},
        {"role": "link", "text": "10042", "bbox": [190, 250, 240, 265], "obscured": True},
    ]})
    cap = store.load("member.savings_balance.read")
    field, by = resolve(cap.steps[4].target, els)   # recorded as label "Search Value:"
    assert by == "label" and field is not None
    assert els[1].obscured
