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

"""Agent Runtime operations that the public Agent Platform SDK doesn't expose.

The SDK's public ``create()`` / ``update()`` block until the deploy finishes and
never return the operation, which ``--no-wait`` and ``deploy --status`` need.
They also can't take a custom update mask.
"""

import time
from typing import Any

from google.agents.cli._agent_platform_types import types

POLL_INTERVAL_SECONDS = 10.0


def build_config(client: Any, *, mode: str, **kwargs: Any) -> dict[str, Any]:
    """Build the create/update request config, including its update mask."""
    return client.agent_engines._create_config(mode=mode, **kwargs)


def start_create(client: Any, *, config: dict[str, Any]) -> str:
    """Start creating an agent engine and return the operation name without waiting."""
    return _operation_name(client.agent_engines._create(config=config))


def start_update(client: Any, *, name: str, config: dict[str, Any]) -> str:
    """Start updating an agent engine and return the operation name without waiting."""
    return _operation_name(client.agent_engines._update(name=name, config=config))


def get_operation(client: Any, *, operation_name: str) -> types.AgentEngineOperation:
    """Fetch the current state of an agent engine operation."""
    return client.agent_engines._get_agent_operation(operation_name=operation_name)


def wait_for_operation(
    client: Any,
    *,
    operation_name: str,
    poll_interval_seconds: float = POLL_INTERVAL_SECONDS,
) -> types.AgentEngineOperation:
    """Poll an agent engine operation until it is done and return it."""
    operation = get_operation(client, operation_name=operation_name)
    while not operation.done:
        time.sleep(poll_interval_seconds)
        operation = get_operation(client, operation_name=operation_name)
    return operation


def _operation_name(operation: types.AgentEngineOperation) -> str:
    name = getattr(operation, "name", None)
    if not name:
        raise RuntimeError("Agent Platform returned an operation without a name.")
    return name
