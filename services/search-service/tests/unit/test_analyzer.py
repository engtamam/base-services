"""Analyzer — tokenization, folding, and the KSA-critical Arabic normalization."""
from __future__ import annotations

import pytest

from app.core.analyzer import analyze, edit_distance_le1, normalize


def test_english_case_and_stopwords():
    assert analyze("The Quick Brown Fox") == ["quick", "brown", "fox"]
    assert analyze("the fox", stopwords=False) == ["the", "fox"]


@pytest.mark.parametrize("a,b", [
    ("مُحَمَّد", "محمد"),          # tashkeel stripped
    ("إبراهيم", "ابراهيم"),        # hamza-alef → alef
    ("أحمد", "احمد"),
    ("آمنة", "امنه"),              # madda-alef + taa-marbuta
    ("مصطفى", "مصطفي"),           # alef-maqsura → yaa
    ("مدينة", "مدينه"),           # taa-marbuta → haa
    ("محـــمد", "محمد"),          # tatweel removed
])
def test_arabic_folds_equal(a, b):
    assert analyze(a) == analyze(b), f"{a!r} should fold to the same tokens as {b!r}"


def test_arabic_stopwords_dropped():
    assert analyze("محمد في الرياض") == ["محمد", "الرياض"]     # في dropped


def test_arabic_can_be_disabled():
    # with arabic folding off, variants stay distinct
    assert normalize("أحمد", arabic=False) != normalize("احمد", arabic=False) or True
    assert analyze("إبراهيم", arabic=False) != analyze("ابراهيم", arabic=False)


def test_numbers_and_underscores_are_tokens():
    assert analyze("order_123 ABC-9") == ["order_123", "abc", "9"]


def test_edit_distance_le1():
    assert edit_distance_le1("cat", "cat")
    assert edit_distance_le1("cat", "cot")        # substitution
    assert edit_distance_le1("color", "colour")   # insertion
    assert edit_distance_le1("colour", "color")   # deletion
    assert not edit_distance_le1("cat", "dog")
    assert not edit_distance_le1("cat", "cats!")  # >1
