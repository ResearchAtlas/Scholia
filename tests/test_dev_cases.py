"""The Chinese and cross-language development cases (S1-19; status.md's S1-18 item (9); tickets 70 and 72
E3): the committed sets' shape and labels, and their runner (evals/dev_cases.py) with tier 1's arms, each
language reported apart. The runner's test uses S1-16's stand-in helper and tier 1's synthetic QASPER
file; no model, download or benchmark file."""

import json

import pytest

from evals import dev_cases, tier1
from test_tier1_eval import _sets, manifest_for, opener, reranking

HAN = range(0x4E00, 0xA000)


def test_the_committed_sets_are_thirty_cases_each_in_chinese_and_labeled():
    cross = dev_cases.load("cross_language.json")
    assert len(cross["cases"]) == 30 and len({c["question_id"] for c in cross["cases"]}) == 30
    subset = set(tier1.load_manifest()["levels"]["subset"]["benchmarks"]["qasper"]["papers"])
    for case in cross["cases"]:
        assert case["paper"] in subset, case["id"]  # tier 1's small fixed subset, so the set is prepared
        assert any(ord(ch) in HAN for ch in case["question_zh"]), case["id"]
    assert "CC BY 4.0" in cross["about"] and "do not establish the Chinese result" in cross["about"]
    chinese = dev_cases.load("chinese_documents.json")
    documents = {d["id"]: d for d in chinese["documents"]}
    assert len(documents) == 30 == len(chinese["cases"]) and "do not establish the Chinese result" in chinese["about"]
    for case in chinese["cases"]:
        assert 0 <= case["paragraph"] < len(documents[case["document"]]["paragraphs"]), case["id"]
        assert all(any(ord(ch) in HAN for ch in text) for text in (case["question"], documents[case["document"]]["title"]))


@pytest.mark.asyncio
async def test_each_set_runs_with_the_arms_and_reports_each_language_apart(tmp_path, monkeypatch):
    import helper_timings
    monkeypatch.setattr(tier1, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(tier1, "machine", lambda: {"chip": "test"})
    monkeypatch.setattr(tier1, "commit", lambda: {"sha": "test", "modified": False})
    monkeypatch.setattr(helper_timings, "sockets", lambda pid: [])
    real = dev_cases.load
    cross = {"cases": [{"id": "xl-01", "paper": "0001.00001", "question_id": "q-cores", "question_zh": "岩芯是在哪里取的？"},
                       {"id": "xl-02", "paper": "0001.00001", "question_id": "q-burial", "question_zh": "埋藏速率随沉积物供给怎样变化？"}]}
    monkeypatch.setattr(dev_cases, "load", lambda name: cross if name == "cross_language.json" else real(name))
    sets = tmp_path / "sets"
    _sets(sets)
    manifest = manifest_for(sets)
    files = tier1.verify(manifest, sets, ["qasper"])["qasper"]
    out = tmp_path / "dev.json"
    arms = {"keyword_only": tier1.keyword_only, "rerank": tier1.rerank, "hybrid": tier1.hybrid}
    results = await dev_cases.run(manifest, files, out, opener(before_yield=reranking()), arms)
    assert results["status"] == "finished" and json.loads(out.read_text())["status"] == "finished"
    assert "do not establish the Chinese result" in results["note"]
    assert results["gaps"] == [] and all(entry["zh"]["complete"] for entry in results["chinese_documents"]["arms"].values())
    part = results["cross_language"]
    assert part["cases"] == 2 and part["document_language"] == "en"
    for arm in arms:
        assert part["arms"][arm]["en"]["recall@8"]["n"] == part["arms"][arm]["zh"]["recall@8"]["n"] == 2
        assert part["arms"][arm]["zh_minus_en"]["recall@8"]["n"] == 2 and "scores" not in part["arms"][arm]["en"]
    zh = results["chinese_documents"]
    assert zh["documents"] == zh["cases"] == 30 and zh["document_language"] == "zh"
    assert all(zh["arms"][arm]["zh"]["recall@8"]["n"] == 30 for arm in arms)
    assert zh["arms"]["keyword_only"]["zh"]["fallbacks"] == {}  # the reference arm's own outcome is not a fallback


@pytest.mark.asyncio
async def test_a_case_whose_question_has_no_usable_evidence_is_refused_and_the_file_says_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(tier1, "POLL_SECONDS", 0.05)
    monkeypatch.setattr(tier1, "commit", lambda: {"sha": "test", "modified": False})
    monkeypatch.setattr(tier1, "machine", lambda: {"chip": "test"})
    cross = {"cases": [{"id": "xl-01", "paper": "0001.00001", "question_id": "q-none", "question_zh": "谁资助的？"}]}
    monkeypatch.setattr(dev_cases, "load", lambda name: cross)
    sets = tmp_path / "sets"
    _sets(sets)
    manifest = manifest_for(sets)
    out = tmp_path / "dev.json"
    with pytest.raises(tier1.Refused, match="xl-01"):
        await dev_cases.run(manifest, tier1.verify(manifest, sets, ["qasper"])["qasper"], out, opener(),
                            {"hybrid": tier1.hybrid})
    assert json.loads(out.read_text())["status"] == "incomplete"
