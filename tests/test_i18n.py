from string import Formatter

import pytest

from backend import i18n


def _placeholders(template: str) -> set[str]:
    return {name for _, name, _, _ in Formatter().parse(template) if name is not None}


def test_templates_have_the_same_keys_and_placeholders_in_both_languages():
    english, chinese = i18n.TEMPLATES["en"], i18n.TEMPLATES["zh-CN"]
    assert set(i18n.TEMPLATES) == {"en", "zh-CN"}
    assert english.keys() == chinese.keys()
    for key in english:
        assert _placeholders(english[key]) == _placeholders(chinese[key]), key


def test_render_fills_the_template_for_the_language():
    assert i18n.render("import.report", "en", imported=3, total=5) == "3 of 5 references imported"
    assert i18n.render("import.report", "zh-CN", imported=3, total=5) == "已导入 3 条参考文献，共 5 条"


def test_render_falls_back_to_english(monkeypatch):
    monkeypatch.setitem(i18n.TEMPLATES, "zh-CN", {})
    assert i18n.render("disclosure.heading", "zh-CN") == "Statement on the use of AI tools"
    assert i18n.render("disclosure.heading", "fr") == "Statement on the use of AI tools"


@pytest.mark.parametrize("setting", ["en", "zh-CN"])
def test_resolve_language_keeps_an_explicit_language(setting):
    assert i18n.resolve_language(setting, "fr_FR") == setting


@pytest.mark.parametrize("locale", ["zh", "zh-CN", "zh_TW", "zh-Hans-CN", "zh_CN.UTF-8", "ZH-hk"])
def test_resolve_language_maps_any_chinese_system_locale_to_zh_cn(locale):
    assert i18n.resolve_language("system", locale) == "zh-CN"


@pytest.mark.parametrize("locale", ["en_US.UTF-8", "en-GB", "fr-FR", "zu", "C", "", None])
def test_resolve_language_maps_every_other_system_locale_to_english(locale):
    assert i18n.resolve_language("system", locale) == "en"
