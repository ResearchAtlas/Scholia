import stat

import pytest

from backend import settings
from backend.settings import load_instructions, load_settings


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_missing_personal_file_gives_defaults(tmp_path):
    loaded = load_settings(tmp_path)
    assert loaded.warnings == []
    assert loaded.values["ui"]["language"] == "system"
    assert loaded.values["ui"]["follow_up"] == "steer"
    assert loaded.values["ui"]["layout"] == {"sidebar_width": 248, "sidebar_open": True, "panel_share": 0.5}
    assert loaded.values["budget"]["conversation_usd"] == 10
    assert loaded.values["subagents"]["at_once"] == 3
    assert loaded.values["subagents"]["tool_calls"] == 30
    assert loaded.values["limits"]["agent_steps"] == 12
    assert loaded.values["limits"]["tool_calls"] == 40
    assert loaded.values["context"]["research_evidence"] == 12000
    assert loaded.values["retrieval"]["keep"] == 8
    assert not (tmp_path / "config.toml").exists()  # loading never creates the file


def test_file_values_override_defaults(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[ui]\nlanguage = "zh-CN"\n\n[ui.layout]\nsidebar_width = 300\n\n'
        '[providers.openrouter]\nkind = "openrouter"\nwindows = { "openai/gpt-5.1" = 200000 }\n'
    )
    loaded = load_settings(tmp_path)
    assert loaded.warnings == []
    assert loaded.values["ui"]["language"] == "zh-CN"
    assert loaded.values["ui"]["layout"]["sidebar_width"] == 300
    assert loaded.values["ui"]["layout"]["sidebar_open"] is True
    assert loaded.values["providers"] == {"openrouter": {"kind": "openrouter", "windows": {"openai/gpt-5.1": 200000}}}


def test_invalid_value_falls_back_with_key_and_line(tmp_path):
    (tmp_path / "config.toml").write_text(
        "# personal settings\n"
        "[ui]\n"
        'language = "fr-secret-value"\n'
        "\n"
        "[ui.layout]\n"
        "sidebar_open = true\n"
        "sidebar_width = 9000\n"
        "\n"
        "[budget]\n"
        "conversation_usd = true\n"
    )
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["language"] == "system"
    assert loaded.values["ui"]["layout"]["sidebar_width"] == 248
    assert loaded.values["budget"]["conversation_usd"] == 10
    assert len(loaded.warnings) == 3
    language, width, budget = loaded.warnings
    assert "ui.language" in language and "line 3" in language
    assert "ui.layout.sidebar_width" in width and "line 7" in width
    assert "budget.conversation_usd" in budget and "line 10" in budget
    assert not any("fr-secret-value" in w or "9000" in w for w in loaded.warnings)  # values never echoed


def test_wrong_shapes_fall_back(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[ui]\nlayout = 5\n[ui.language]\nx = "en"\n[models]\nefforts = "high"\n'
    )
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["layout"]["sidebar_width"] == 248
    assert loaded.values["ui"]["language"] == "system"
    assert "efforts" not in loaded.values["models"]
    assert [w.split(":")[0] for w in loaded.warnings] == [
        "config.toml line 2", "config.toml line 4", "config.toml line 6",
    ]


def test_unparseable_file_gives_defaults_with_line(tmp_path):
    (tmp_path / "config.toml").write_text('[ui]\nlanguage = "en"\nbroken = = 1\n')
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["language"] == "system"
    assert len(loaded.warnings) == 1 and "line 3" in loaded.warnings[0]


def test_unknown_keys_load_without_warning(tmp_path):
    (tmp_path / "config.toml").write_text('[zotero]\nenabled = true\n[ui]\nfuture_option = 1\n')
    loaded = load_settings(tmp_path)
    assert loaded.warnings == []
    assert "zotero" not in loaded.values


def write_project_file(root, text, project_id="p1"):
    folder = root / "projects" / project_id
    folder.mkdir(parents=True)
    (folder / "config.toml").write_text(text)


def test_project_file_has_its_own_keys_and_defaults(tmp_path):
    write_project_file(tmp_path, '[project]\ntarget_venue = "Nature"\n[limits]\nagent_steps = 20\n')
    loaded = load_settings(tmp_path, "p1")
    assert loaded.warnings == []
    assert loaded.values["project"] == {"target_venue": "Nature", "budget_usd": 50}
    assert loaded.values["limits"] == {"agent_steps": 20}  # only overrides; the rest inherit


def test_project_file_cannot_define_providers_keys_or_subagents(tmp_path):
    write_project_file(tmp_path, (
        "[project]\n"
        "budget_usd = 20\n"
        "api_key = 'sk-hidden'\n"
        "[providers.openrouter]\n"
        "kind = 'openrouter'\n"
        "[subagents]\n"
        "at_once = 9\n"
        "[mcp_server.env]\n"
        "GITHUB_TOKEN = 'ghp-hidden'\n"
    ))
    loaded = load_settings(tmp_path, "p1")
    assert loaded.values == {"project": {"budget_usd": 20}}
    assert len(loaded.warnings) == 4
    assert ["line 3" in loaded.warnings[0], "line 5" in loaded.warnings[1],
            "line 7" in loaded.warnings[2], "line 9" in loaded.warnings[3]] == [True] * 4
    assert "project.api_key" in loaded.warnings[0]
    assert "providers.openrouter.kind" in loaded.warnings[1]
    assert "subagents.at_once" in loaded.warnings[2]
    assert not any("hidden" in w for w in loaded.warnings)


def test_personal_file_never_supplies_a_secret(tmp_path):
    (tmp_path / "config.toml").write_text("[providers.openrouter]\nkind = 'openrouter'\napi_key = 'sk-hidden'\n")
    loaded = load_settings(tmp_path)
    assert loaded.values["providers"] == {"openrouter": {"kind": "openrouter"}}
    assert len(loaded.warnings) == 1
    assert "providers.openrouter.api_key" in loaded.warnings[0] and "line 3" in loaded.warnings[0]
    assert "credential store" in loaded.warnings[0]


@pytest.mark.parametrize("project_id", ["", ".", "..", "../x", "a/b", "a\\b"])
def test_project_id_cannot_leave_the_projects_folder(tmp_path, project_id):
    with pytest.raises(ValueError):
        load_settings(tmp_path, project_id)


COMMENTED = (
    "# My settings, edited by hand\n"
    "[ui]\n"
    'language = "en"  # English for now\n'
    "\n"
    "[zotero]  # a section this version does not read yet\n"
    "enabled = true\n"
)


def test_save_keeps_comments_and_unknown_keys(tmp_path):
    (tmp_path / "config.toml").write_text(COMMENTED)
    loaded = load_settings(tmp_path)
    loaded.save({"ui.follow_up": "queue", "ui.layout.sidebar_width": 300,
                 'providers.openrouter.windows."openai/gpt-5.1"': 200000})
    text = (tmp_path / "config.toml").read_text()
    assert text.startswith(COMMENTED.split("\n[zotero]")[0])
    assert "# a section this version does not read yet\nenabled = true\n" in text
    reread = load_settings(tmp_path)
    assert reread.warnings == []
    assert reread.values["ui"]["follow_up"] == "queue"
    assert reread.values["ui"]["layout"]["sidebar_width"] == 300
    assert reread.values["providers"]["openrouter"]["windows"] == {"openai/gpt-5.1": 200000}
    assert loaded.values == reread.values  # the saved object is up to date
    loaded.save({"ui.language": "zh-CN"})  # and can save again


def test_save_creates_owner_only_file_and_folders(tmp_path):
    load_settings(tmp_path, "p1").save({"project.target_venue": "Nature"})
    assert mode(tmp_path / "projects") == 0o700
    assert mode(tmp_path / "projects" / "p1") == 0o700
    assert mode(tmp_path / "projects" / "p1" / "config.toml") == 0o600
    load_settings(tmp_path).save({"ui.language": "en"})
    assert mode(tmp_path / "config.toml") == 0o600


@pytest.mark.parametrize("before, after", [(0o644, 0o600), (0o600, 0o600), (0o400, 0o400)])
def test_save_never_broadens_existing_permissions(tmp_path, before, after):
    path = tmp_path / "config.toml"
    path.write_text("[ui]\n")
    path.chmod(before)
    load_settings(tmp_path).save({"ui.language": "en"})
    assert mode(path) == after


def test_save_refuses_when_file_changed_on_disk(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ui]\nlanguage = "en"\n')
    loaded = load_settings(tmp_path)
    path.write_text('[ui]\nlanguage = "zh-CN"\n')  # edited in another program
    with pytest.raises(settings.SettingsChanged):
        loaded.save({"ui.follow_up": "queue"})
    assert path.read_text() == '[ui]\nlanguage = "zh-CN"\n'
    reloaded = load_settings(tmp_path)  # the caller reloads, asks, then saves
    reloaded.save({"ui.follow_up": "queue"})
    assert load_settings(tmp_path).values["ui"] == {
        "language": "zh-CN", "follow_up": "queue",
        "layout": {"sidebar_width": 248, "sidebar_open": True, "panel_share": 0.5},
    }


def test_save_refuses_when_file_created_or_deleted_since_load(tmp_path):
    path = tmp_path / "config.toml"
    loaded = load_settings(tmp_path)
    path.write_text("[ui]\n")
    with pytest.raises(settings.SettingsChanged):
        loaded.save({"ui.language": "en"})
    loaded = load_settings(tmp_path)
    path.unlink()
    with pytest.raises(settings.SettingsChanged):
        loaded.save({"ui.language": "en"})
    assert not path.exists()


def test_save_refuses_to_overwrite_an_unparseable_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("broken = = 1\n")
    with pytest.raises(ValueError):
        load_settings(tmp_path).save({"ui.language": "en"})
    assert path.read_text() == "broken = = 1\n"


@pytest.mark.parametrize("project_id, updates", [
    (None, {"ui.language": "fr"}),
    (None, {"ui.layout": 5}),
    (None, {"providers.openrouter.api_key": "sk-test"}),
    (None, {"providers.openrouter": {"kind": "openrouter", "token": "sk-test"}}),
    (None, {"providers.openrouter.api_key.value": "sk-test"}),
    ("p1", {"providers.openrouter.kind": "openrouter"}),
    ("p1", {"subagents.at_once": 2}),
    ("p1", {"providers": {}}),
    ("p1", {"mcp_server.env.GITHUB_TOKEN": "ghp-test"}),
])
def test_save_refuses_invalid_personal_only_and_secret_keys(tmp_path, project_id, updates):
    loaded = load_settings(tmp_path, project_id)
    with pytest.raises(ValueError) as error:
        loaded.save({"ui.follow_up" if project_id is None else "project.template": "queue", **updates})
    assert "sk-test" not in str(error.value) and "ghp-test" not in str(error.value)
    assert not loaded.path.exists()  # nothing was written, not even the valid update


def test_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text(COMMENTED)
    loaded = load_settings(tmp_path)

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(settings.os, "replace", fail)
    with pytest.raises(OSError):
        loaded.save({"ui.language": "zh-CN"})
    assert path.read_text() == COMMENTED
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]  # no temporary file left


def test_save_into_a_table_defined_out_of_order(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[ui]\nlanguage = "en"\n\n[budget]\nconversation_usd = 5\n\n[ui.layout]\nsidebar_width = 300\n')
    load_settings(tmp_path).save({"ui.layout.sidebar_width": 280, "ui.follow_up": "queue"})
    assert path.read_text() == (
        '[ui]\nlanguage = "en"\nfollow_up = "queue"\n\n[budget]\nconversation_usd = 5\n\n'
        '[ui.layout]\nsidebar_width = 280\n'
    )


def test_warning_lines_skip_multiline_values(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[helper]\n"
        'model_source = """\n'
        "idle_stop_minutes = 0\n"  # inside the string, not a key
        '"""\n'
        "[subagents]\n"
        "models = [\n"
        '  "a", "b",  # [ not a bracket\n'
        '  "c", "d", "e", "f",\n'
        "]\n"
        'ui.language = "xx"\n'  # a dotted key inside [subagents]
        "[models.efforts]\n"
        '"openai/gpt-5.1" = 3\n'
        "[providers.local]\n"
        "windows = { m = 100 }\n"
    )
    loaded = load_settings(tmp_path)
    assert [w.split(":")[0] + ":" + w.split(":")[1].split()[0] for w in loaded.warnings] == [
        "config.toml line 6:subagents.models",
        "config.toml line 12:models.efforts.openai/gpt-5.1",
        "config.toml line 14:providers.local.windows.m",
    ]
    assert loaded.values["helper"]["model_source"] == "idle_stop_minutes = 0\n"


def write_instructions(root, personal=None, project=None):
    if personal is not None:
        (root / "AGENTS.md").write_bytes(personal.encode() if isinstance(personal, str) else personal)
    if project is not None:
        (root / "projects" / "p1").mkdir(parents=True)
        (root / "projects" / "p1" / "AGENTS.md").write_bytes(project.encode() if isinstance(project, str) else project)


def test_instructions_are_personal_then_project(tmp_path):
    write_instructions(tmp_path, personal="Write in British English.\n", project="Cite APA 7.\n")
    assert load_instructions(tmp_path, "p1") == ("Write in British English.\n\n\nCite APA 7.\n", [])
    assert load_instructions(tmp_path) == ("Write in British English.\n", [])


def test_missing_instructions_are_empty(tmp_path):
    assert load_instructions(tmp_path, "p1") == ("", [])
    write_instructions(tmp_path, project="Only the project.")
    assert load_instructions(tmp_path, "p1") == ("Only the project.", [])


def test_instructions_cap_at_32_kib_combined_with_warning(tmp_path):
    personal = "p" * 20_000
    project = "é" * 10_000  # 2 bytes each in UTF-8
    write_instructions(tmp_path, personal=personal, project=project)
    text, warnings = load_instructions(tmp_path, "p1")
    assert len(text.encode()) <= 32 * 1024
    assert len(text.encode()) >= 32 * 1024 - 1  # cut on a character boundary, not short
    assert text.startswith(personal + "\n\n") and set(text[len(personal) + 2:]) == {"é"}
    assert len(warnings) == 1 and "32 KiB" in warnings[0]


def test_instructions_exactly_at_the_cap_are_kept_whole(tmp_path):
    write_instructions(tmp_path, personal="a" * (32 * 1024))
    assert load_instructions(tmp_path) == ("a" * (32 * 1024), [])


def test_instructions_that_are_not_utf8_load_with_a_warning(tmp_path):
    write_instructions(tmp_path, personal=b"caf\xe9\n")
    text, warnings = load_instructions(tmp_path)
    assert text == "caf�\n"
    assert len(warnings) == 1 and "UTF-8" in warnings[0]


def test_unreadable_files_never_stop_loading(tmp_path):
    (tmp_path / "config.toml").mkdir()  # cannot be read as a file
    (tmp_path / "AGENTS.md").mkdir()
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["language"] == "system"
    assert len(loaded.warnings) == 1 and "could not be read" in loaded.warnings[0]
    text, warnings = load_instructions(tmp_path)
    assert text == "" and len(warnings) == 1 and "could not be read" in warnings[0]
    with pytest.raises(OSError):
        loaded.save({"ui.language": "en"})
