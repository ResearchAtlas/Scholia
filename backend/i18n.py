"""The interface language, and the few texts the backend writes for people.

API errors carry a stable code that the interface translates from its own
catalogs. The texts here are the ones the backend writes itself, with English
and Simplified Chinese templates using {name} placeholders.
"""

import re

LANGUAGES = ("en", "zh-CN")

TEMPLATES = {
    "en": {
        "disclosure.heading": "Statement on the use of AI tools",
        "import.report": "{imported} of {total} references imported",
        "export.untitled": "Untitled conversation",
        "export.researcher": "Researcher",
        "export.other_conversation": "From another conversation",
        "export.no_answer": "No answer: {status}",
        "export.status.succeeded": "Done",
        "export.status.failed": "Failed",
        "export.status.cancelled": "Cancelled",
        "export.status.interrupted": "Interrupted",
    },
    "zh-CN": {
        "disclosure.heading": "人工智能工具使用声明",
        "import.report": "已导入 {imported} 条参考文献，共 {total} 条",
        "export.untitled": "未命名对话",
        "export.researcher": "研究者",
        "export.other_conversation": "来自另一个对话",
        "export.no_answer": "没有回答：{status}",
        "export.status.succeeded": "已完成",
        "export.status.failed": "失败",
        "export.status.cancelled": "已取消",
        "export.status.interrupted": "已中断",
    },
}


def resolve_language(setting: str, system_locale: str | None) -> str:
    """The `[ui] language` setting as "en" or "zh-CN".

    "system" follows the OS locale: any Chinese locale maps to zh-CN and every
    other locale to English.
    """
    if setting in LANGUAGES:
        return setting
    return "zh-CN" if re.match(r"zh(?:[-_]|$)", system_locale or "", re.IGNORECASE) else "en"


def render(key: str, language: str, **params) -> str:
    """The template for the language, or the English one when it has none."""
    templates = TEMPLATES.get(language, {})
    template = templates[key] if key in templates else TEMPLATES["en"][key]
    return template.format(**params)
