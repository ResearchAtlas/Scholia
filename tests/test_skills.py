"""Skills (slice-1 spec sections 6.2, 11 and 14; S1-19c): the bundled `scholia.skills` loader, hash-checked
on every load; `use_skill` and `read_skill_file` (pinned file ids only, final review F06); ACTIVE SKILLS from
the next turn, once per conversation; `help` with `open_place`; `literature-review`'s approved files and
notices; `GET /api/skills`. Model calls go to recorded tool-calling replies; nothing leaves the process."""

import json
import pathlib
import re
import shutil

import pytest

from agentic import Scripted, call, tool_messages
from backend import agent, skills
from cited import cited, conversation_in, events_of, rows, with_papers
from scholia_app import send
from test_search import WAGES

LITERATURE = skills.ROOT / "literature-review"


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    """A copy of the bundled skills the test may change, used by the app in its place."""
    root = tmp_path / "scholia.skills"
    shutil.copytree(skills.ROOT, root)
    monkeypatch.setattr(skills, "ROOT", root)
    return root


def test_the_bundled_skills_load_with_every_file_matching_the_manifest():
    loaded, problems = skills.load()
    assert problems == {} and set(loaded) == {"help", "literature-review"}
    assert all(s.description and s.zh["description"] != s.description for s in loaded.values())  # Chinese beside


@pytest.mark.parametrize("change, problem", [
    (lambda root: (root / "help" / "SKILL.md").write_bytes((root / "help" / "SKILL.md").read_bytes() + b" "), "changed"),
    (lambda root: (root / "help" / "notes.md").write_text("added"), "extra_file"),
    (lambda root: (root / "help" / "SKILL.zh-CN.md").unlink(), "missing_file"),
    (lambda root: ((root / "help" / "SKILL.zh-CN.md").unlink(),
                   (root / "help" / "SKILL.zh-CN.md").symlink_to(root / "help" / "SKILL.md")), "symlink"),
])
def test_a_changed_extra_missing_or_symlinked_file_makes_its_skill_unavailable(bundle, change, problem):
    change(bundle)
    loaded, problems = skills.load()
    assert problems == {"help": problem} and set(loaded) == {"literature-review"}  # the other is unaffected


def test_read_skill_file_takes_only_a_pinned_id_and_checks_its_hash(bundle):
    [(first, path, _), _] = skills.load()[0]["literature-review"].files
    assert skills.read_file(first)[0].startswith("# Screening")
    for refused in ("references/screening.md", "literature-review/references/screening.md", "../help/SKILL.md",
                    "literature-review#9", "literature-review#0", "help#1", "/etc/passwd", "literature-review#1/.."):
        with pytest.raises(ValueError, match="unknown_file"):
            skills.read_file(refused)
    (bundle / "literature-review" / path).write_text("changed")
    with pytest.raises(ValueError):  # the skill is unavailable now: none of its ids reads
        skills.read_file(first)


def test_literature_review_ships_only_approved_instructions_and_references_with_its_notice():
    files = {p.relative_to(LITERATURE).as_posix() for p in LITERATURE.rglob("*") if p.is_file()}
    assert files == {"SKILL.md", "SKILL.zh-CN.md", "LICENSE.md", "NOTICE.md", "references/screening.md",
                     "references/synthesis.md"}  # no scripts, assets, figures or export toolchain
    assert "Copyright (c) 2025 K-Dense Inc." in (LITERATURE / "LICENSE.md").read_text()
    notice = (LITERATURE / "NOTICE.md").read_text()
    assert "a92663a0214b3b9fa1ba6b958284ffd919c00a0c" in notice and "version 1.11" in notice
    body = skills.load()[0]["literature-review"].body
    for absent in ("update_progress", "find_papers", "parallel-cli", "OPENROUTER_API_KEY", "allowed-tools"):
        assert absent not in body
    for tool in ("answer_from_library", "search_passages", "read_passage"):
        assert tool in body


def test_the_help_skill_names_only_places_open_place_can_open():
    for skill in skills.load()[0]["help"], :
        for text in (skill.body, skill.zh["body"]):
            named = set(re.findall(r"`((?:library|settings\.[a-z]+))`", text))
            assert named == set(agent.PLACES)  # each place it names opens, and it names every one


@pytest.mark.asyncio
async def test_use_skill_returns_its_body_in_tags_and_file_ids_and_records_its_activation(tmp_path):
    provider = Scripted([call("use_skill", name="literature-review")],
                        [call("read_skill_file", call_id="c2", file_id="literature-review#2")], "Reviewed.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "Use the literature review skill on my papers")
        assert {"type": "skill", "name": "literature-review"} in stream
        [activated] = tool_messages(provider.agent_steps[1])
        assert activated["content"].startswith('<skill name="literature-review">\n# Literature review over the library')
        assert "name: literature-review" not in activated["content"]  # no front matter
        assert "- literature-review#1: references/screening.md" in activated["content"]
        read = tool_messages(provider.agent_steps[2])[-1]
        assert read["content"].startswith("# Synthesis")
        run_id = stream[0]["run_id"]
        [skill] = [d for k, d, _ in await events_of(client, run_id) if k == "skill_activated"]
        assert skill["name"] == "literature-review" and len(skill["sha256"]) == 64
        assert json.loads((await rows(client, "SELECT skills FROM runs WHERE id = ?", run_id))[0][0]) == ["literature-review"]


@pytest.mark.asyncio
async def test_use_skill_refuses_an_unknown_name_or_a_path(tmp_path):
    provider = Scripted([call("use_skill", call_id="a", name="brainstorm"), call("use_skill", call_id="b", name="../help"),
                         call("list_materials", call_id="ok")],
                        [call("read_skill_file", call_id="c", file_id="references/screening.md")], "Done.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "Which papers do I have?")
        unknown, path, _, file_path = tool_messages(provider.agent_steps[2])
        assert [json.loads(m["content"])["error"] for m in (unknown, path, file_path)] == ["validation_error"] * 3


@pytest.mark.asyncio
async def test_a_skill_activated_mid_turn_joins_active_skills_from_the_next_turn_once(tmp_path):
    provider = Scripted([call("use_skill", name="help")], "First.", [call("use_skill", call_id="c2", name="help")],
                        "Second.", "Third.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        await send(client, conversation, "How do I add a reference file?")
        await send(client, conversation, "And Zotero?")
        await send(client, conversation, "Thanks")
        first_turn_second_step = provider.agent_steps[1]["messages"][0]["content"]
        assert "## SKILL CATALOG\n- help: " in first_turn_second_step
        assert "## ACTIVE SKILLS" not in first_turn_second_step  # in its tool result for the rest of that turn
        later = provider.agent_steps[2]["messages"][0]["content"]
        assert later.count('<skill name="help">') == 1
        last = provider.agent_steps[-1]["messages"][0]["content"]
        assert last.count('<skill name="help">') == 1  # loaded once per conversation


@pytest.mark.asyncio
async def test_open_place_opens_one_place_from_its_list_and_nothing_else(tmp_path):
    provider = Scripted([call("use_skill", name="help")],
                        [call("open_place", call_id="a", place="library"), call("open_place", call_id="b", place="file:///etc")],
                        "Opened.")
    async with cited(tmp_path, provider) as client:
        project, _ = await with_papers(client, WAGES)
        conversation = await conversation_in(client, project)
        stream = await send(client, conversation, "How do I add a reference file?")
        assert [e for e in stream if e["type"] == "open_place"] == [{"type": "open_place", "place": "library"}]
        opened, refused = tool_messages(provider.agent_steps[2])[-2:]
        assert opened["content"].startswith("Opened library") and json.loads(refused["content"])["error"] == \
            "validation_error"


@pytest.mark.asyncio
async def test_the_skill_catalog_lists_each_skill_in_the_interface_language_and_its_state(tmp_path, bundle):
    async with cited(tmp_path) as client:
        english = (await client.get("/api/skills")).json()["skills"]
        assert [(s["name"], s["enabled"], s["source"], s["status"]) for s in english] == [
            ("help", True, "scholia.skills", "ok"), ("literature-review", True, "scholia.skills", "ok")]
        chinese = (await client.get("/api/skills?language=zh-CN")).json()["skills"]
        assert chinese[0]["description"].startswith("如何使用")
        (bundle / "help" / "SKILL.md").write_text("changed")
        listed = (await client.get("/api/skills")).json()["skills"]
        assert {"name": "help", "description": None, "source": "scholia.skills", "enabled": False,
                "status": "changed"} in listed


@pytest.mark.asyncio
async def test_the_skill_catalog_fits_its_budget():
    from backend.context import count
    loaded, _ = skills.load()
    assert count("## SKILL CATALOG\n" + "\n\n".join(agent.catalog(loaded))) <= 1000  # section 13 (K1)


def test_the_license_audit_ships_literature_reviews_notice_with_the_app():
    import sys
    sys.path.insert(0, str(skills.ROOT.parents[2] / "tools"))
    import license_audit
    shipped = {(pathlib.Path(source).name, dest) for source, dest in license_audit.notice_datas()}
    assert {("LICENSE.md", "licenses/K-Dense scientific-agent-skills"),
            ("NOTICE.md", "licenses/K-Dense scientific-agent-skills")} <= shipped
    assert license_audit.component(license_audit.KDENSE)[0] == "MIT"
