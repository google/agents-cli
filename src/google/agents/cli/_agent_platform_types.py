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

"""Agent Platform SDK types, imported through the SDK's public path.

Use ``types.EvalCase``, ``types.evals.AgentEvent``, etc. from here rather than
importing ``agentplatform._genai`` directly.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # The public `agentplatform.types` is served by an untyped module
    # `__getattr__`, which type checkers resolve to Unknown. Point them at the
    # module it returns so the SDK models stay type-checked.
    from agentplatform._genai import types  # noqa: TID251
else:
    from agentplatform import types

__all__ = ["types"]
