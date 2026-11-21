"""The backend's JSON: the standard library's, but no value it decodes or text it writes holds half a surrogate pair.

Half a pair comes from a JSON escape ("\\ud83d") or from raw bytes (json.loads decodes bytes with surrogatepass);
outside text (a lookup, Zotero, a provider) can send either. Decoded, it is the replacement character, as a file's text
gets (S1-13, S1-14), so neither the database nor a JSON answer is ever handed one; a record stored with one by an
earlier build is healed when Python reads it (design review AA, Option 4). Every backend module imports this as
`from backend import whole_json as json`, and tests/test_whole_json.py fails for one that imports json itself.
Nothing but the standard library: the reading child imports it.
"""

import json
import re

_HALF_PAIR = re.compile("[\ud800-\udfff]")
# half a pair as it is, or an escape that may be one (\ud800 to \udfff): text with neither takes one scan and no more
_MAY_HOLD = re.compile(r"[\ud800-\udfff]|\\u[dD][89a-fA-F]")


def whole_text(value):
    """A decoded JSON value with each half of a surrogate pair in its text the replacement character; the same object
    when it holds none. May raise RecursionError, as decoding does."""
    text = json.dumps(value, ensure_ascii=False)
    return value if not _HALF_PAIR.search(text) else json.loads(_HALF_PAIR.sub("\ufffd", text))


def loads(source, **options):
    """json.loads, with half a pair the replacement character. The options go to json.loads. When the text holds half a
    pair or any escape in \\ud800 to \\udfff (a whole pair's too), or comes as bytes, the value is written and read again, so
    an option that makes a value JSON cannot write (a Decimal, a hook's object) raises TypeError then; no backend module
    passes one here (outbound_gate keeps json for its hook)."""
    value = json.loads(source, **options)
    return value if isinstance(source, str) and not _MAY_HOLD.search(source) else whole_text(value)


def dumps(value, **options):
    """json.dumps of the value with half a pair the replacement character. When the text holds one, or any escape in
    \\ud800 to \\udfff, the value is cleaned through JSON first: `default` and `cls` are not used for that, so a value
    only they can write raises TypeError, and int keys are sorted as text; no backend module passes them here."""
    text = json.dumps(value, **options)
    return text if not _MAY_HOLD.search(text) else json.dumps(whole_text(value), **options)
