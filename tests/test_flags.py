"""Problem-word flags on seller text (autoplius_stats.text_flags)."""

import pytest

from autoplius_stats import text_flags


@pytest.mark.parametrize("text, expected", [
    ("Daužtas priekis", ["damaged"]),
    ("Po avarijos, reikia remonto", ["damaged"]),
    ("Variklis neužsiveda", ["not running"]),
    ("Parduodu be TA", ["no inspection"]),
    ("Eksportui", ["export"]),
    ("Ardoma dalimis", ["parts"]),
    ("Lizingo perėmimas", ["lease transfer"]),
    ("Be dokumentų", ["no documents"]),
])
def test_problem_words_are_flagged(text, expected):
    assert text_flags(text) == expected


@pytest.mark.parametrize("text", [
    "Nedaužtas, be defektų",
    "Puiki būklė, jokių gedimų",
    "TA iki 2027",
    "Nebuvo daužtas",
    "Automobilis tvarkingas, servisuotas",
])
def test_denials_and_clean_text_are_not_flagged(text):
    assert text_flags(text) == []


def test_title_and_description_are_both_checked():
    assert text_flags("BMW 320 eksportui", None) == ["export"]
    assert text_flags(None, None) == []
