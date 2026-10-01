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
    (tmp_path / "config.toml").write_text('[future]\nenabled = true\n[ui]\nfuture_option = 1\n')
    loaded = load_settings(tmp_path)
    assert loaded.warnings == []
    assert "future" not in loaded.values and "future_option" not in loaded.values["ui"]


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
        "[context]\n"
        r'system_rules = """an escaped \""" does not close it' "\n"
        "user_memory = 5\n"  # inside the string, not a key
        '"""\n'
        "user_memory = -1\n"
        "[retrieval]\n"
        r"keep = '''C:\'''" "\n"  # a literal string: the backslash escapes nothing
        "rrf_k = 0\n"
    )
    loaded = load_settings(tmp_path)
    assert [w.split(":")[0] + ":" + w.split(":")[1].split()[0] for w in loaded.warnings] == [
        "config.toml line 6:subagents.models",
        "config.toml line 12:models.efforts.openai/gpt-5.1",
        "config.toml line 14:providers.local.windows.m",
        "config.toml line 16:context.system_rules",
        "config.toml line 19:context.user_memory",
        "config.toml line 21:retrieval.keep",
        "config.toml line 22:retrieval.rrf_k",
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


def test_empty_table_where_a_value_belongs_falls_back(tmp_path):
    (tmp_path / "config.toml").write_text("[ui.language]\n[subagents.models]\n[ui.layout]\n[providers.openrouter]\n")
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["language"] == "system"
    assert loaded.values["subagents"]["models"] == []
    assert loaded.values["ui"]["layout"]["sidebar_width"] == 248  # an empty table where a table belongs is fine
    assert loaded.values["providers"] == {"openrouter": {}}
    assert [w.split(";")[0] for w in loaded.warnings] == [
        "config.toml line 1: ui.language is not valid",
        "config.toml line 2: subagents.models is not valid",
    ]


@pytest.mark.parametrize("project_id, updates", [
    (None, {"integrations": [{"api_key": "sk-test"}]}),
    (None, {"zotero": {"accounts": [{"name": "x", "token": "sk-test"}]}}),
    (None, {"keys.openrouter": "sk-test"}),
    ("p1", {"keys.openrouter": "sk-test"}),
    ("p1", {"keys": {}}),
    ("p1", {"mcp_server": {"servers": [{"env": {"GITHUB_TOKEN": "sk-test"}}]}}),
])
def test_save_refuses_secrets_at_any_depth(tmp_path, project_id, updates):
    loaded = load_settings(tmp_path, project_id)
    with pytest.raises(ValueError) as error:
        loaded.save(updates)
    assert "sk-test" not in str(error.value)
    assert not loaded.path.exists()


def test_hand_written_secrets_are_ignored_but_kept(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[zotero]\nenabled = true\napi_key = "sk-hand"\n[[integrations]]\ntoken = "sk-hand"\n')
    loaded = load_settings(tmp_path)
    assert loaded.values["zotero"] == {"enabled": True}
    assert [w.split(";")[0] for w in loaded.warnings] == [
        "config.toml line 3: zotero.api_key looks like a secret",
        "config.toml line 4: integrations looks like a secret",
    ]
    loaded.save({"ui.language": "en"})
    assert path.read_text().count("sk-hand") == 2  # the researcher's own text is never deleted


def test_dict_updates_apply_leaf_by_leaf(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('# mine\n[ui]\nlanguage = "en"  # note\nfollow_up = "queue"\ncustom = 1\n')
    load_settings(tmp_path).save({"ui": {"follow_up": "steer", "layout": {"sidebar_open": False}}})
    assert path.read_text() == (
        '# mine\n[ui]\nlanguage = "en"  # note\nfollow_up = "steer"\ncustom = 1\n'
        "\n[ui.layout]\nsidebar_open = false\n"
    )


def test_sections_without_a_defined_shape_are_exposed_as_given(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[zotero]\nenabled = true\nlibrary = 12\n[discovery]\nsources = ["openalex"]\n'
        "[extensions.aab-cite]\nstyle = { name = \"apa\" }\n[mcp]\nenabled = [\"x\"]\n"
    )
    write_project_file(tmp_path, '[mcp]\nenabled = ["files"]\n[mcp_server]\nexpose = true\n[zotero]\nenabled = true\n')
    personal = load_settings(tmp_path)
    assert personal.warnings == []
    assert personal.values["zotero"] == {"enabled": True, "library": 12}
    assert personal.values["discovery"] == {"sources": ["openalex"]}
    assert personal.values["extensions"] == {"aab-cite": {"style": {"name": "apa"}}}
    assert "mcp" not in personal.values  # a project section
    project = load_settings(tmp_path, "p1")
    assert project.values["mcp"] == {"enabled": ["files"]}
    assert project.values["mcp_server"] == {"expose": True}
    assert "zotero" not in project.values  # a personal section
    personal.save({"zotero.library": 13, "extensions.aab-cite.style.name": "mla"})
    assert load_settings(tmp_path).values["extensions"] == {"aab-cite": {"style": {"name": "mla"}}}
    assert load_settings(tmp_path).values["zotero"]["library"] == 13


def test_out_of_range_numbers_fall_back(tmp_path):
    huge = "9" * 400
    (tmp_path / "config.toml").write_text(f"[budget]\nconversation_usd = {huge}\n[limits]\nagent_steps = {huge}\n")
    loaded = load_settings(tmp_path)
    assert loaded.values["budget"]["conversation_usd"] == 10
    assert loaded.values["limits"]["agent_steps"] == 12
    assert [w.split(";")[0] for w in loaded.warnings] == [
        "config.toml line 2: budget.conversation_usd is not valid",
        "config.toml line 4: limits.agent_steps is not valid",
    ]
    with pytest.raises(ValueError):
        loaded.save({"limits.agent_steps": 2**63})


def test_warning_lines_decode_quoted_keys(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[models.efforts]\n"
        'ok = "high"\n'
        '"a\\"b.c" = 3\n'
        '"x\\u0041" = 4\n'
        "'lit.eral' = 5\n"
        '"has = sign" = 6\n'
        '[providers."we\\"ird"]\n'
        "default_window = 1\n"
    )
    loaded = load_settings(tmp_path)
    assert [w.split(";")[0] for w in loaded.warnings] == [
        'config.toml line 3: models.efforts.a"b.c is not valid',
        "config.toml line 4: models.efforts.xA is not valid",
        "config.toml line 5: models.efforts.lit.eral is not valid",
        "config.toml line 6: models.efforts.has = sign is not valid",
        'config.toml line 8: providers.we"ird.default_window is not valid',
    ]


@pytest.mark.parametrize("project_id, text, key, line, left", [
    (None, '# zotero settings\nzotero = 1\n', "zotero", 2, {}),
    (None, 'discovery = "on"\n', "discovery", 1, {}),
    (None, '# none yet\nextensions = 1\n', "extensions", 2, {}),
    (None, '[extensions]\nfoo = "x"\n[extensions.bar]\nx = 1\n', "extensions.foo", 2, {"extensions": {"bar": {"x": 1}}}),
    ("p1", '[project]\ntemplate = "t"\n', None, None, {}),  # control: nothing to warn about
    ("p1", 'mcp = false\n', "mcp", 1, {}),
    ("p1", '\nmcp_server = 1\n[mcp]\nenabled = ["files"]\n', "mcp_server", 2, {"mcp": {"enabled": ["files"]}}),
])
def test_open_sections_must_be_tables(tmp_path, project_id, text, key, line, left):
    if project_id:
        write_project_file(tmp_path, text)
    else:
        (tmp_path / "config.toml").write_text(text)
    loaded = load_settings(tmp_path, project_id)
    open_sections = {"zotero", "discovery", "extensions", "mcp", "mcp_server"}
    assert {k: v for k, v in loaded.values.items() if k in open_sections} == left
    expected = [] if key is None else [f"{loaded.label} line {line}: {key} is not valid; ignored"]
    assert loaded.warnings == expected
    assert loaded.path.read_text() == text  # the file's own bytes are never changed by loading


@pytest.mark.parametrize("project_id, updates", [
    (None, {"zotero": 1}),
    (None, {"discovery": "on"}),
    (None, {"extensions": 1}),
    (None, {"extensions.foo": "x"}),
    (None, {"extensions": {"foo": "x"}}),
    ("p1", {"mcp": False}),
    ("p1", {"mcp_server": 1}),
])
def test_save_refuses_scalars_at_open_section_roots(tmp_path, project_id, updates):
    loaded = load_settings(tmp_path, project_id)
    with pytest.raises(ValueError):
        loaded.save(updates)
    assert not loaded.path.exists()


@pytest.mark.parametrize("name", [
    "AUTHORIZATION", "auth", "basic-auth", "Bearer", "cookie", "session.cookies", "credential", "credentials",
    "passphrase", "private_key", "Private-Key", "access_key", "client_secret", "apiKey", "accessToken", "passwd",
])
def test_credential_field_names_are_secrets(tmp_path, name):
    key = ".".join(f'"{part}"' for part in name.split("."))
    write_project_file(tmp_path, f'[mcp_server.env]\nPATH = "/bin"\n{key} = "Bearer hidden"\n')
    loaded = load_settings(tmp_path, "p1")
    assert loaded.values["mcp_server"] == {"env": {"PATH": "/bin"}}
    assert len(loaded.warnings) == 1 and "line 3" in loaded.warnings[0] and "looks like a secret" in loaded.warnings[0]
    with pytest.raises(ValueError):
        loaded.save({f"mcp_server.env.{key}": "Bearer hidden"})


def test_identifiers_are_not_mistaken_for_secrets(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[providers.local-key]\nkind = "openai-compatible"\ndefault_window = 8192\nnote = 1\n'
        '[providers.local-key.windows]\n"my-token" = 16384\n'
        '[models.efforts]\n"vendor/secret" = "high"\n'
        '[extensions.aab-auth]\nstyle = "apa"\n'
    )
    write_project_file(tmp_path, '[mcp.servers.github]\ncommand = "gh-mcp"\n')
    personal = load_settings(tmp_path)
    assert personal.warnings == []
    assert personal.values["providers"] == {
        "local-key": {"kind": "openai-compatible", "default_window": 8192, "windows": {"my-token": 16384}},
    }
    assert personal.values["models"]["efforts"] == {"vendor/secret": "high"}
    assert personal.values["extensions"] == {"aab-auth": {"style": "apa"}}
    personal.save({"providers.local-key.kind": "openrouter", 'models.efforts."vendor/secret"': "low"})
    assert load_settings(tmp_path).values["providers"]["local-key"]["kind"] == "openrouter"
    project = load_settings(tmp_path, "p1")
    assert project.warnings == []
    assert project.values["mcp"] == {"servers": {"github": {"command": "gh-mcp"}}}
    project.save({"mcp.servers.github.command": "gh"})


def test_warning_lines_count_only_toml_line_endings(tmp_path):
    (tmp_path / "config.toml").write_text(
        "[helper]\r\n"
        'model_source = "a b\u0085c"  # comment   here\r\n'
        "idle_stop_minutes = 0\n"
    )
    loaded = load_settings(tmp_path)
    assert loaded.values["helper"]["model_source"] == "a b\u0085c"
    assert [w.split(":")[0] for w in loaded.warnings] == ["config.toml line 3"]


@pytest.mark.parametrize("dotted, value", [
    ("ui.language", "en"),
    ("budget.conversation_usd", 20),
    ("privacy.trim_bodies_after_days", 30),
    ("models.efforts.gpt", "high"),
    ("zotero.enabled", True),
])
def test_project_save_refuses_personal_only_settings(tmp_path, dotted, value):
    loaded = load_settings(tmp_path, "p1")
    with pytest.raises(ValueError, match="personal setting"):
        loaded.save({dotted: value})
    assert not loaded.path.exists()


def test_project_save_keeps_project_and_unknown_paths(tmp_path):
    loaded = load_settings(tmp_path, "p1")
    loaded.save({"project.target_venue": "Nature", "limits.agent_steps": 20, "ui.panel": "library",
                 "future.option": 1})
    reread = load_settings(tmp_path, "p1")
    assert reread.warnings == []
    assert reread.values["project"]["target_venue"] == "Nature"
    assert reread.values["limits"] == {"agent_steps": 20}
    assert reread.values["ui"] == {"panel": "library"}
    assert "[future]\noption = 1\n" in loaded.path.read_text()  # in neither schema: kept as today


def test_personal_only_settings_in_a_project_file_are_ignored_with_a_warning(tmp_path):
    write_project_file(tmp_path, '[ui]\npanel = "library"\nlanguage = "en"\n[budget]\nconversation_usd = 20\n')
    loaded = load_settings(tmp_path, "p1")
    assert loaded.values["ui"] == {"panel": "library"}
    assert "budget" not in loaded.values
    assert [w.split(";")[0] for w in loaded.warnings] == [
        "project config.toml line 3: ui.language is a personal setting and cannot be set in a project file",
        "project config.toml line 5: budget.conversation_usd is a personal setting and cannot be set in a project file",
    ]


@pytest.mark.parametrize("text, key", [
    ('[zotero.api_key]\nvalue = "hidden"\n', "zotero.api_key.value"),
    ('[extensions.foo.auth]\nvalue = "hidden"\n', "extensions.foo.auth.value"),
    ('[discovery.sources.openalex.credentials]\nuser = "hidden"\n', "discovery.sources.openalex.credentials.user"),
])
def test_table_names_inside_open_sections_are_field_names(tmp_path, text, key):
    (tmp_path / "config.toml").write_text(text)
    loaded = load_settings(tmp_path)
    assert loaded.warnings == [f"config.toml line 2: {key} looks like a secret; keys belong in the credential store; ignored"]
    assert not {"zotero", "discovery"} & set(loaded.values) and "extensions" not in loaded.values
    with pytest.raises(ValueError, match="looks like a secret"):
        loaded.save({key: "hidden"})
    assert loaded.path.read_text() == text


def test_extension_ids_are_identifiers_even_with_secret_words(tmp_path):
    (tmp_path / "config.toml").write_text('[extensions.my-key]\nstyle = "apa"\n[extensions.aab-auth.options]\nmode = 1\n')
    loaded = load_settings(tmp_path)
    assert loaded.warnings == []
    assert loaded.values["extensions"] == {"my-key": {"style": "apa"}, "aab-auth": {"options": {"mode": 1}}}
    loaded.save({"extensions.my-key.style": "mla", "extensions.aab-auth.options.mode": 2})
    assert load_settings(tmp_path).values["extensions"]["aab-auth"] == {"options": {"mode": 2}}


DOTTED = ".".join(f"k{i}" for i in range(99))  # near the parser's 100-level limit per key


def nested(levels):
    return "{" + f"{DOTTED} = " + (nested(levels - 1) if levels > 1 else "1") + "}"


def test_deeply_nested_settings_never_stop_loading(tmp_path):
    path = tmp_path / "config.toml"
    text = f'[ui]\nlanguage = "en"\n[zotero]\nx = {nested(40)}\n'  # about 4,000 tables deep, still valid TOML
    path.write_text(text)
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["language"] == "system" and "zotero" not in loaded.values
    assert loaded.warnings == ["config.toml: nested too deeply to read; using the defaults"]
    with pytest.raises(ValueError):
        loaded.save({"ui.follow_up": "queue"})
    assert path.read_text() == text


def test_moderately_nested_settings_load(tmp_path):
    (tmp_path / "config.toml").write_text(f"[zotero]\nx = {nested(2)}\n")  # about 200 tables deep
    loaded = load_settings(tmp_path)
    assert loaded.warnings == []
    node = loaded.values["zotero"]["x"]
    for _ in range(2):
        for i in range(99):
            node = node[f"k{i}"]
    assert node == 1


def test_secret_tables_and_env_names_are_refused_and_ignored(tmp_path):
    personal = load_settings(tmp_path)
    with pytest.raises(ValueError, match="looks like a secret"):
        personal.save({"zotero.api_key": {"value": "sk-synthetic"}})
    project = load_settings(tmp_path, "p1")
    with pytest.raises(ValueError, match="looks like a secret"):
        project.save({"mcp_server.env.GITHUB_TOKEN": "x"})
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "config.toml").write_text('[zotero.api_key]\nvalue = "sk-synthetic"\n')
    write_project_file(tmp_path, '[mcp_server.env]\nGITHUB_TOKEN = "x"\n')
    personal, project = load_settings(tmp_path), load_settings(tmp_path, "p1")
    assert "zotero" not in personal.values and "mcp_server" not in project.values
    assert personal.warnings == [
        "config.toml line 2: zotero.api_key.value looks like a secret; keys belong in the credential store; ignored"]
    assert project.warnings == [
        "project config.toml line 2: mcp_server.env.GITHUB_TOKEN looks like a secret; "
        "keys belong in the credential store; ignored"]


def test_nested_dotted_inline_tables_never_stop_loading(tmp_path):
    # Twenty nested inline tables, each holding a fifty-part dotted key: about 2 KB of valid TOML.
    dotted = ".".join("k" for _ in range(50))
    text = "[zotero]\nx = " + f"{{{dotted} = " * 20 + "1" + "}" * 20 + "\n"
    assert 1500 < len(text) < 2500
    (tmp_path / "config.toml").write_text(text)
    loaded = load_settings(tmp_path)
    assert loaded.warnings == ["config.toml: nested too deeply to read; using the defaults"]
    assert "zotero" not in loaded.values and loaded.values["ui"]["language"] == "system"


def test_recursion_anywhere_in_reading_falls_back(tmp_path, monkeypatch):
    (tmp_path / "config.toml").write_text('[ui]\nlanguage = "en"\n')

    def too_deep(*args):
        raise RecursionError

    monkeypatch.setattr(settings, "_check", too_deep)  # the walk over the file's values
    loaded = load_settings(tmp_path)
    assert loaded.values["ui"]["language"] == "system"
    assert loaded.warnings == ["config.toml: nested too deeply to read; using the defaults"]
    with pytest.raises(ValueError):
        loaded.save({"ui.language": "zh-CN"})
