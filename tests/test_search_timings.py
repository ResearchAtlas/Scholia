"""The search timing tool (tools/search_timings.py; S1-17, ticket 70's measurement rules): what it
holds in the process it measures, and what its desktop results say the network sandbox covers."""

import argparse
import inspect

from tools import helper_timings, search_timings


def test_the_synthetic_papers_are_made_one_at_a_time_as_they_are_added(monkeypatch):
    """No corpus is held in the measured process: a paper is made only when it is taken."""
    made, real = [], search_timings._paragraph
    monkeypatch.setattr(search_timings, "_paragraph", lambda *args: made.append(1) or real(*args))
    _, papers, queries = search_timings.corpus(argparse.Namespace(corpus="synthetic", size="100k", papers=None))
    assert made == [] and len(queries) == 200
    name, text = next(papers)
    assert name == "synthetic-00001.md" and text.startswith(b"# ") and len(made) == 24  # four sections of six
    next(papers)
    assert len(made) == 48


def test_the_desktop_results_say_what_the_network_sandbox_covers():
    source = inspect.getsource(helper_timings._run_desktop)
    assert 'results["network_isolation"]' in source
    assert "WebKit" in source and "loopback-only.sb confines this Python process and its children" in source
