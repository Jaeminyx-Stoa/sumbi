"""Check Korean particles in the vendored practice guidance."""

import json
from pathlib import Path
import re
import unittest


CATALOG = Path(__file__).resolve().parents[2] / "sumbi/catalog/practices.json"
LINK = re.compile(r"\[([^\]\n]+)\]\([^\)\n]+\)(으로|로|을|를|이|가|은|는|과|와)?")
PARTICLE_PAIRS = (("을", "를"), ("이", "가"), ("은", "는"), ("과", "와"), ("으로", "로"))


def expected_particle(label, particle):
    final_syllable = ord(label[-1])
    if not 0xAC00 <= final_syllable <= 0xD7A3:
        raise ValueError("Link label must end in a Hangul syllable for automatic agreement checking")
    final_consonant = (final_syllable - 0xAC00) % 28
    consonant_form, vowel_form = next(pair for pair in PARTICLE_PAIRS if particle in pair)
    # Directional 로 follows both a vowel and the final consonant ㄹ (index 8).
    if consonant_form == "으로" and final_consonant == 8:
        return vowel_form
    return consonant_form if final_consonant else vowel_form


class KoreanPracticeTextTests(unittest.TestCase):
    def test_particle_agreement_after_every_korean_markdown_link(self):
        # Exercise every pair with synthetic vowel, consonant and ㄹ endings,
        # including incorrect input particles so a checker cannot just echo them.
        for label, forms in (
            ("사과", ("를", "가", "는", "와", "로")),
            ("책", ("을", "이", "은", "과", "으로")),
            ("길", ("을", "이", "은", "과", "로")),
        ):
            for pair, expected in zip(PARTICLE_PAIRS, forms):
                for particle in pair:
                    with self.subTest(label=label, particle=particle):
                        self.assertEqual(expected_particle(label, particle), expected)

        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        links_checked = 0
        for practice in catalog["practices"]:
            for file in practice["files"]:
                for field in ("text", "text_without_links"):
                    text = file.get(field, {}).get("ko", "")
                    for match in LINK.finditer(text):
                        label, particle = match.groups()
                        if particle is None:
                            continue  # Links without these particles need no agreement check.
                        with self.subTest(practice=practice["id"], path=file["path"],
                                          field=field, label=label):
                            self.assertEqual(particle, expected_particle(label, particle))
                        links_checked += 1
        self.assertGreater(links_checked, 0, "No Korean link particles were checked")
