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

"""Legacy evalset and eval_config migration for the upgrade command."""

import json
import logging
import pathlib
from typing import Any

_LEGACY_EVALSETS_DIR = "tests/eval/evalsets"
NEW_DATASETS_DIR = "tests/eval/datasets"
_LEGACY_EVALSET_GLOB = "*.evalset.json"
_LEGACY_EVAL_CONFIG = "tests/eval/eval_config.json"
_NEW_EVAL_CONFIG = "tests/eval/eval_config.yaml"
_EVAL_MIGRATION_URL = (
    "https://google.github.io/agents-cli/reference/eval-dataset-migration/"
)


def _convert_eval_case(old_case: dict[str, Any]) -> dict[str, Any]:
    new_case: dict[str, Any] = {}
    case_id = old_case.get("eval_id") or old_case.get("eval_case_id")
    if case_id:
        new_case["eval_case_id"] = case_id

    conversation = old_case.get("conversation") or []
    if not conversation:
        return new_case

    if len(conversation) == 1:
        turn = conversation[0]
        user_parts = (turn.get("user_content") or {}).get("parts") or []
        new_case["prompt"] = {"role": "user", "parts": user_parts}
        final_response = turn.get("final_response")
        if final_response:
            new_case["reference"] = {
                "response": {
                    "role": "model",
                    "parts": final_response.get("parts") or [],
                }
            }
        return new_case

    events: list[dict[str, Any]] = []
    last_idx = len(conversation) - 1
    for i, turn in enumerate(conversation):
        user_parts = (turn.get("user_content") or {}).get("parts") or []
        events.append(
            {
                "author": "user",
                "content": {"role": "user", "parts": user_parts},
            }
        )
        final_response = turn.get("final_response")
        if not final_response:
            continue
        if i < last_idx:
            events.append(
                {
                    "author": "agent",
                    "content": {
                        "role": "model",
                        "parts": final_response.get("parts") or [],
                    },
                }
            )
        else:
            new_case["reference"] = {
                "response": {
                    "role": "model",
                    "parts": final_response.get("parts") or [],
                }
            }
    new_case["agent_data"] = {
        "turns": [{"turn_index": 0, "events": events}],
    }
    return new_case


def _convert_eval_set(old_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "eval_cases": [
            _convert_eval_case(c) for c in (old_payload.get("eval_cases") or [])
        ]
    }


def _legacy_to_new_filename(legacy_name: str) -> str:
    stem = legacy_name[: -len(".evalset.json")]
    return f"{stem}-dataset.json"


def migrate_legacy_evalsets(project_dir: pathlib.Path, dry_run: bool = False) -> None:
    """Convert tests/eval/evalsets/*.evalset.json to tests/eval/datasets/*-dataset.json.

    No-op when the legacy directory is absent. Skips destination files
    that already exist, and warns only when one holds different JSON than
    the converted file. Reports legacy files it cannot read or convert.
    Does not delete the legacy directory; the user removes it after
    verifying the conversion.
    """
    import click

    legacy_dir = project_dir / _LEGACY_EVALSETS_DIR
    if not legacy_dir.is_dir():
        return

    legacy_files = sorted(legacy_dir.glob(_LEGACY_EVALSET_GLOB))
    if not legacy_files:
        return

    output_dir = project_dir / NEW_DATASETS_DIR

    if dry_run:
        count = len(legacy_files)
        noun = "file" if count == 1 else "files"
        click.secho(
            f"  ▸ [Dry run] {count} legacy eval {noun} would be migrated "
            f"to {NEW_DATASETS_DIR}/",
            fg="yellow",
            dim=True,
        )
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    converted = 0
    conflicting: list[str] = []
    failed: list[tuple[str, str]] = []

    for src in legacy_files:
        dest = output_dir / _legacy_to_new_filename(src.name)
        try:
            old_payload = json.loads(src.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            failed.append((src.name, str(e)))
            continue
        try:
            new_payload = _convert_eval_set(old_payload)
        except (AttributeError, TypeError):
            failed.append((src.name, "not in the legacy .evalset.json format"))
            continue
        if dest.exists():
            if _read_json_or_none(dest) != new_payload:
                conflicting.append(dest.name)
            continue
        dest.write_text(json.dumps(new_payload, indent=2) + "\n", encoding="utf-8")
        converted += 1

    if converted:
        noun = "file" if converted == 1 else "files"
        click.secho(
            f"  ▸ Migrated {converted} legacy eval {noun} to {NEW_DATASETS_DIR}/. "
            "Run `agents-cli eval generate` to populate the trace.",
            fg="cyan",
            dim=True,
        )
    if conflicting:
        logging.warning(
            "Did NOT migrate %d legacy eval file(s) — these files already exist "
            "in %s/ and differ from the converted ones: %s. The legacy file(s) "
            "remain in %s/ unconverted. Reconcile manually before deleting the "
            "legacy directory.",
            len(conflicting),
            NEW_DATASETS_DIR,
            ", ".join(conflicting),
            _LEGACY_EVALSETS_DIR,
        )
    if failed:
        for name, err in failed:
            click.secho(f"  ▸ Failed to migrate {name}: {err}", fg="red", dim=True)
        click.secho(
            f"  ▸ See migration guide: {_EVAL_MIGRATION_URL}",
            fg="yellow",
            dim=True,
        )
    if converted and not conflicting and not failed:
        click.secho(
            f"  ▸ All legacy evalsets migrated. You can delete "
            f"{_LEGACY_EVALSETS_DIR}/ once you've verified the converted files.",
            fg="cyan",
            dim=True,
        )


def _read_json_or_none(path: pathlib.Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def warn_legacy_eval_config(project_dir: pathlib.Path) -> None:
    """Warn if a legacy tests/eval/eval_config.json is left over after upgrade."""
    if not (project_dir / _LEGACY_EVAL_CONFIG).is_file():
        return

    logging.warning(
        "Found legacy %s. Grading now reads %s; your custom criteria will NOT "
        "apply until migrated. The two schemas differ, so this is not "
        "auto-converted. See migration guide: %s",
        _LEGACY_EVAL_CONFIG,
        _NEW_EVAL_CONFIG,
        _EVAL_MIGRATION_URL,
    )
