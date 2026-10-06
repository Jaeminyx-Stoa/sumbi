"""Normalizer limits must be explicit and leave measurements byte-identical."""

from pathlib import Path
import unittest

from .normalizers import Normalizer


class NormalizerTests(unittest.TestCase):
    def test_counts_order_numbers_ids_and_pseudonyms_are_untouched(self):
        text = '{"count": 17, "duration": 1.23456789, "id": "D2", "project_key": "project_0123456789", "order": [3, 1, 2]}\n'
        self.assertEqual(Normalizer(Path("/synthetic/temp")).text(text), text)

    def test_only_registered_clock_values_are_replaced(self):
        normalizer = Normalizer(Path("/synthetic/temp"))
        normalizer.add("2030-01-15T00:00:00Z", "<APPLY_TIME>")
        self.assertEqual(normalizer.text("2030-01-15T00:00:00Z 2030-01-01T00:00:00Z"),
                         "<APPLY_TIME> 2030-01-01T00:00:00Z")

    def test_unknown_and_conflicting_variable_fields_fail(self):
        normalizer = Normalizer(Path("/synthetic/temp"))
        for value in (None, 17, ""):
            with self.assertRaises(ValueError):
                normalizer.add(value, "<TIME>")
        normalizer.add("clock", "<TIME>")
        with self.assertRaises(ValueError):
            normalizer.add("clock", "<OTHER>")
