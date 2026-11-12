# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""The conversation and user of the turn that is running."""

from __future__ import annotations

from contextvars import ContextVar

# The running turn's conversation, scoped to the caller. Tools can key
# per-conversation state on it.
current_conversation: ContextVar[str | None] = ContextVar(
    "current_conversation", default=None
)
# The running turn's user: the authenticated caller, else one named after the
# conversation. Tools can key per-user state on it. Unset outside a turn, where
# get() raises LookupError.
current_user: ContextVar[str] = ContextVar("current_user")
