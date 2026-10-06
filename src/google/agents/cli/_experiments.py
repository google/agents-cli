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

import json
import logging
import os
from typing import Any, NamedTuple


class Experiment(NamedTuple):
    label: str
    value_type: type
    default_value: Any


# Central registry of experiments: new experiment flags get added here.
_REGISTRY: dict[str, Experiment] = {
    # Hides the `build` command until Go (compiled-language) support launches.
    "build_command": Experiment("build_command", bool, True),
}


# String spellings accepted for boolean experiments, e.g. '{"x": "false"}'.
# bool("false") is True, so strings can't go through the plain type cast.
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_STRINGS = frozenset({"0", "false", "no", "off"})


def _cast(exp: Experiment, val: Any) -> Any:
    """Cast an override value to the experiment's type."""
    if exp.value_type is bool and isinstance(val, str):
        normalized = val.strip().lower()
        if normalized in _TRUE_STRINGS:
            return True
        if normalized in _FALSE_STRINGS:
            return False
        raise ValueError(f"expected true/false, 1/0, yes/no or on/off, got {val!r}")
    return exp.value_type(val)


def resolve_experiment(label: str) -> Any:
    """Returns the value for the given experiment label."""
    if label not in _REGISTRY:
        raise ValueError(f"Unknown experiment: {label}")

    exp = _REGISTRY[label]

    # Check for environment variable override
    env_val = os.environ.get("AGENTS_CLI_EXPERIMENTS")
    if env_val:
        try:
            overrides = json.loads(env_val)
            if label in overrides:
                val = overrides[label]
                return _cast(exp, val)
        except Exception as e:
            logging.warning(
                f"Failed to apply AGENTS_CLI_EXPERIMENTS override for '{label}', "
                f"using default ({exp.default_value}). Error: {e}"
            )

    return exp.default_value
