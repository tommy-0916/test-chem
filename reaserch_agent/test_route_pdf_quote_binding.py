"""Literal PDF quote binding never guesses a source span."""

from __future__ import annotations

import unittest

from reaserch_agent.route_pdf_quote_binding import bind_pdf_quote


class PdfQuoteBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.blocks = [
            ("pdf:p1:b2-p1:b2", "Solution C was prepared with 6.25 mmol"),
            ("pdf:p1:b3-p1:b3", "Na2CO3 in 50 mL water."),
            ("pdf:p1:b4-p1:b4", "The mixture was stirred."),
        ]

    def test_unique_quote_crosses_two_blocks_and_rebinds_locator(self) -> None:
        binding, reason = bind_pdf_quote(
            self.blocks, "6.25\n mmol   Na2CO3 in 50 mL",
            asserted_block_locator="pdf:p1:b2-p1:b2",
        )
        self.assertEqual(reason, "")
        self.assertEqual(binding.locator, "pdf:p1:b2-p1:b3")
        self.assertEqual((binding.first_block_index, binding.last_block_index), (0, 1))

    def test_wrong_anchor_paraphrase_and_duplicate_fail_closed(self) -> None:
        _, wrong_anchor = bind_pdf_quote(
            self.blocks, "6.25 mmol Na2CO3",
            asserted_block_locator="pdf:p1:b4-p1:b4",
        )
        self.assertEqual(wrong_anchor, "fact_block_locator_not_in_excerpt_span")
        _, paraphrase = bind_pdf_quote(self.blocks, "6.25 mmol carbonate")
        self.assertEqual(paraphrase, "fact_excerpt_not_in_block")
        repeated = self.blocks + [("pdf:p1:b5-p1:b5", "6.25 mmol Na2CO3")]
        _, ambiguous = bind_pdf_quote(repeated, "6.25 mmol Na2CO3")
        self.assertEqual(ambiguous, "fact_excerpt_ambiguous_in_group")

    def test_four_block_and_cross_group_matches_are_rejected(self) -> None:
        blocks = [
            (f"pdf:p1:b{index}-p1:b{index}", word)
            for index, word in enumerate(("one", "two", "three", "four"), start=2)
        ]
        _, too_long = bind_pdf_quote(blocks, "one two three four")
        self.assertEqual(too_long, "fact_excerpt_span_too_long")
        _, not_in_group = bind_pdf_quote(self.blocks[:1], "6.25 mmol Na2CO3")
        self.assertEqual(not_in_group, "fact_excerpt_not_in_block")

    def test_one_parser_marked_caption_can_interrupt_a_cross_page_sentence(self) -> None:
        blocks = [
            ("pdf:p2:b138-p2:b138",
             "electrode. A glassy carbon electrode was thoroughly cleaned and"),
            ("pdf:p3:b1-p3:b1", "Scheme 1. Synthesis Protocol for Three Types of LDHs"),
            ("pdf:p3:b2-p3:b2", "polished to a mirror finish before use."),
        ]
        excerpt = (
            "A glassy carbon electrode was thoroughly cleaned and polished "
            "to a mirror finish before use."
        )
        caption = {blocks[1][0]}
        binding, reason = bind_pdf_quote(
            blocks, excerpt, asserted_block_locator=blocks[0][0],
            caption_block_locators=caption,
        )
        self.assertEqual(reason, "")
        self.assertEqual(binding.locator, "pdf:p2:b138-p3:b2")
        _, unmarked = bind_pdf_quote(blocks, excerpt)
        self.assertEqual(unmarked, "fact_excerpt_not_in_block")
        _, caption_anchor = bind_pdf_quote(
            blocks, excerpt, asserted_block_locator=blocks[1][0],
            caption_block_locators=caption,
        )
        self.assertEqual(caption_anchor, "fact_block_locator_not_in_excerpt_span")
        _, caption_as_fact = bind_pdf_quote(
            blocks, "Scheme 1. Synthesis Protocol for Three Types of LDHs",
            caption_block_locators=caption,
        )
        self.assertEqual(caption_as_fact, "fact_excerpt_not_in_block")
        _, caption_in_quote = bind_pdf_quote(
            blocks,
            "cleaned and Scheme 1. Synthesis Protocol for Three Types of LDHs polished",
            caption_block_locators=caption,
        )
        self.assertEqual(caption_in_quote, "fact_excerpt_not_in_block")

    def test_non_caption_prose_and_two_captions_cannot_be_skipped(self) -> None:
        blocks = [
            ("pdf:p1:b1-p1:b1", "The solid was washed and"),
            ("pdf:p1:b2-p1:b2", "heated for 2 h."),
            ("pdf:p1:b3-p1:b3", "dried overnight."),
        ]
        _, ordinary = bind_pdf_quote(blocks, "washed and dried overnight.")
        self.assertEqual(ordinary, "fact_excerpt_not_in_block")
        captions = [
            blocks[0],
            ("pdf:p1:b2-p1:b2", "Figure 1. A caption"),
            ("pdf:p1:b3-p1:b3", "Table 1. Another caption"),
            ("pdf:p1:b4-p1:b4", "dried overnight."),
        ]
        _, double = bind_pdf_quote(
            captions, "washed and dried overnight.",
            caption_block_locators={captions[1][0], captions[2][0]},
        )
        self.assertEqual(double, "fact_excerpt_not_in_block")

    def test_explicit_joiner_seams_allow_only_layout_space_difference(self) -> None:
        hydrate = [
            ("pdf:p2:b58-p2:b58", "Fe(NO3)3·"),
            ("pdf:p2:b59-p2:b59", "9H2O was dissolved."),
        ]
        binding, reason = bind_pdf_quote(hydrate, "Fe(NO3)3·9H2O")
        self.assertEqual(reason, "")
        self.assertEqual(binding.locator, "pdf:p2:b58-p2:b59")
        _, wrong_anchor = bind_pdf_quote(
            hydrate, "9H2O", asserted_block_locator=hydrate[0][0],
        )
        self.assertEqual(wrong_anchor, "fact_block_locator_not_in_excerpt_span")
        slash = [
            ("pdf:p2:b98-p2:b98", "redispersion/"),
            ("pdf:p2:b99-p2:b99", "washing was repeated."),
        ]
        self.assertEqual(bind_pdf_quote(slash, "redispersion/washing")[1], "")
        digits = [
            ("pdf:p1:b1-p1:b1", "heated for 1"),
            ("pdf:p1:b2-p1:b2", "0 min"),
        ]
        self.assertEqual(bind_pdf_quote(digits, "10 min")[1],
                         "fact_excerpt_not_in_block")
        self.assertEqual(bind_pdf_quote(hydrate, "Fe(NO3)3·10H2O")[1],
                         "fact_excerpt_not_in_block")
    def test_same_text_different_chunking_keeps_evidence_verdict(self) -> None:
        """Only the budget verdict may change with layout, never evidence-found."""
        prose = "alpha beta gamma delta epsilon zeta eta theta"
        coarse = [
            ("pdf:p1:b1-p1:b1", "alpha beta gamma delta"),
            ("pdf:p1:b2-p1:b2", "epsilon zeta eta theta"),
        ]
        fine = [
            (f"pdf:p1:b{index}-p1:b{index}", word)
            for index, word in enumerate(prose.split(), start=1)
        ]
        binding, reason = bind_pdf_quote(coarse, prose)
        self.assertEqual(reason, "")
        self.assertEqual(binding.locator, "pdf:p1:b1-p1:b2")
        # The identical quote is still found uniquely under the finer layout;
        # the block budget is reported on its own, not as absent evidence.
        _, fine_reason = bind_pdf_quote(fine, prose)
        self.assertEqual(fine_reason, "fact_excerpt_span_too_long")
        # A span inside the budget binds in both layouts alike.
        short = "beta gamma delta"
        self.assertEqual(bind_pdf_quote(coarse, short)[1], "")
        fine_binding, fine_short = bind_pdf_quote(fine, short)
        self.assertEqual(fine_short, "")
        self.assertEqual(fine_binding.locator, "pdf:p1:b2-p1:b4")
        medium = "beta gamma delta epsilon zeta"
        self.assertEqual(bind_pdf_quote(coarse, medium)[1], "")
        self.assertEqual(bind_pdf_quote(fine, medium)[1],
                         "fact_excerpt_span_too_long")


if __name__ == "__main__":
    unittest.main()
