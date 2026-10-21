# Carried over from AI Advisory Board, backend/evidence.py at commit
# b5d687820e88c10de25a9a2343d3cc478e497524, adapted for Scholia: only the pure helpers slice 1
# section 1.5 names (CITATION, SOURCE_ID and historical_citations); the storage-reading
# functions and the chunking stay behind (review 12, V02).
"""Citation markers in answers: [S<source>.<passage>], each number from 1."""

import re

SOURCE_ID = re.compile(r"[A-Za-z0-9_-]+")
CITATION = re.compile(r"\[S([1-9][0-9]*)\.([1-9][0-9]*)\]")


def historical_citations(text):
    """Change only model-facing projection. The saved answer remains untouched."""
    return CITATION.sub(lambda match: f"(historical citation S{match[1]}.{match[2]}; not current evidence)", text)
