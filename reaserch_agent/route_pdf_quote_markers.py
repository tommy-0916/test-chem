"""Bounded lexical markers for safe quotation shortening.

These regular expressions guard trimming decisions. They are deliberately
narrow: a match means the context cannot be proven safe to drop, so the
trim abstains. They are not a semantic correctness proof, and the full
original quotation is preserved on every trimmed fact for verification.
"""

from __future__ import annotations

import re

NEGATION_OR_CONTRAST = re.compile(
    r"n't\b|\b(?:not|never|neither|nor|without|instead|rather|except|but|"
    r"however|whereas|unlike)\b",
    re.IGNORECASE,
)
RETRACTION_LIMITATION = re.compile(
    r"\b(?:ruled\s+out|rather\s+than|instead\s+of|no\s+longer|at\s+best|"
    r"at\s+most|up\s+to|less\s+than|more\s+than|disproven|disputed|"
    r"questioned|corrected|revised|reinterpreted|recast|retracted|"
    r"presumably|believed|tentative|uncertain|arguably|likely|unlikely|"
    r"possibly|perhaps|maybe|apparent(?:ly)?|seemingly|purported|"
    r"nominal(?:ly)?|estimate[ds]?|estimation|although|though)\b",
    re.IGNORECASE,
)

__all__ = ["NEGATION_OR_CONTRAST", "RETRACTION_LIMITATION"]
