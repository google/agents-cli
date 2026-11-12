# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Readable text for errors an agent server reports as ``str(exception)``."""

from __future__ import annotations

import ast
import json
import re

# `<google.auth.transport.requests._Response object at 0x7f...>`, with the
# comma that separates it from the exception's other args, before or after it.
_OBJECT = r"<[\w.]+ object at 0x[0-9a-fA-F]+>"
_OBJECT_REPR = re.compile(rf",\s*{_OBJECT}|\s*{_OBJECT}\s*,\s*|\s*{_OBJECT}")
# An optional `ErrorType: ` prefix, then a tuple repr of the exception's args.
_ARGS_REPR = re.compile(r"(\w+: )?(\(.*\))", re.DOTALL)
_BYTES_LITERAL = re.compile(r"""\bb('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")""")


def clean_error_text(text: str) -> str:
    """Strip Python reprs from an error message.

    An exception raised with several args, such as google-auth's
    ``RefreshError``, prints as a tuple repr holding object reprs, bytes
    literals and dicts. Returns its args as plain text joined by ``": "``, with
    bytes decoded; other text is returned with only object reprs removed.
    """
    text = _OBJECT_REPR.sub("", text)
    match = _ARGS_REPR.fullmatch(text)
    if match:
        try:
            args = ast.literal_eval(match.group(2))
        except (ValueError, SyntaxError):
            args = None
        if isinstance(args, str | bytes | dict):
            args = (args,)
        if (
            isinstance(args, tuple)
            and args
            and all(isinstance(a, str | bytes | dict) for a in args)
        ):
            text = (match.group(1) or "") + _join_args(args)
    return _BYTES_LITERAL.sub(_decode_bytes_literal, text)


def _join_args(args: tuple[str | bytes | dict, ...]) -> str:
    texts: list[str] = []
    for arg in args:
        if not isinstance(arg, dict):
            texts.append(_decode(arg))
            continue
        # google-auth's OAuth2 errors pass the response body after a message
        # built from its "error" and "error_description"; skip it then.
        error = arg.get("error")
        if not (error and any(str(error) in text for text in texts)):
            texts.append(json.dumps(arg, default=str))
    return ": ".join(texts)


def _decode_bytes_literal(match: re.Match[str]) -> str:
    try:
        return _decode(ast.literal_eval(match.group(0)))
    except (ValueError, SyntaxError):
        return match.group(0)


def _decode(value: str | bytes) -> str:
    # The bytes are HTTP response bodies, not local text, so the system encoding
    # doesn't apply. Google API errors are JSON, which must be UTF-8 (RFC 8259);
    # anything else still prints, with U+FFFD for the bytes that don't decode.
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
