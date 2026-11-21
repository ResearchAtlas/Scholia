"""The backend's JSON answers: Starlette's JSONResponse, but one whose content holds half a surrogate pair is written
with the replacement character in its place, not failed (design review AA, Options 2 and 4). It is the default answer
of both FastAPI apps and the one every route constructs; tests/test_whole_json.py fails for a module that names
JSONResponse itself."""

from fastapi.responses import JSONResponse

from backend.whole_json import whole_text


class WholeJSONResponse(JSONResponse):
    def render(self, content):
        try:
            return super().render(content)
        except UnicodeEncodeError:  # half a pair (a record stored by an earlier build): no cost when the text is whole
            return super().render(whole_text(content))
