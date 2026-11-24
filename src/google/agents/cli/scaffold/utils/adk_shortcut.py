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

"""Handle the removed ``adk@<name>`` shortcut.

It pointed at ``python/agents/<name>`` in adk-samples, since renamed to
adk-recipes and reorganized. A typed shortcut is rejected; one recorded as a
project's ``base_template`` is resolved against a fresh clone, so existing
projects still upgrade.
"""

import logging
import pathlib
import re
import shutil

import click

from .remote_template import (
    ADK_RECIPE_DIRS,
    ADK_RECIPE_INDEX_URLS,
    ADK_RECIPES_REPO_URL,
    ADK_SHORTCUT_PREFIX,
    RemoteTemplateSpec,
    fetch_remote_template,
    is_adk_shortcut,
)
from .upgrade import update_acli_metadata

_BROWSE_RECIPES = "Browse the recipes at:\n" + "\n".join(
    f"  {url}" for url in ADK_RECIPE_INDEX_URLS
)


def reject_adk_shortcut(agent_spec: str) -> None:
    """Reject a typed ``adk@<name>``, pointing at the recipe indexes instead.

    Call before ``parse_agent_spec``. Fails even for a recipe that exists.

    Raises:
        click.UsageError: If ``agent_spec`` uses the ``adk@`` shortcut.
    """
    if is_adk_shortcut(agent_spec):
        raise click.UsageError(
            f"The '{ADK_SHORTCUT_PREFIX}' shortcut is no longer supported. Pass "
            f"the recipe's GitHub URL instead. {_BROWSE_RECIPES}"
        )


def resolve_recorded_adk_shortcut(base_template: str | None) -> str | None:
    """Return the recipe URL for a manifest ``base_template`` of ``adk@<name>``.

    Returns None for any other template. Doesn't write the manifest; see
    ``record_migrated_base_template``.

    Raises:
        click.ClickException: If the name doesn't resolve to exactly one recipe.
    """
    if not base_template or not is_adk_shortcut(base_template):
        return None
    try:
        url = _resolve_recipe_url(base_template)
    except click.ClickException as e:
        raise click.ClickException(
            f"This project was created from '{base_template}', which could not "
            "be resolved. Set base_template in agents-cli-manifest.yaml to the "
            f"recipe's GitHub URL, then rerun.\n{e.message}"
        ) from e
    logging.warning(
        "base_template '%s' in agents-cli-manifest.yaml is deprecated; using %s.",
        base_template,
        url,
    )
    return url


def record_migrated_base_template(project_dir: pathlib.Path, url: str) -> None:
    """Write a migrated spec, such as a resolved ``adk@<name>`` URL, as ``base_template``."""
    updated = update_acli_metadata(project_dir, {}, base_template=url)

    if updated:
        logging.warning("Updated base_template in agents-cli-manifest.yaml to %s.", url)


def _resolve_recipe_url(agent_spec: str) -> str:
    """The adk-recipes URL for a recorded ``adk@<name>``."""
    recipe_name = agent_spec[len(ADK_SHORTCUT_PREFIX) :].strip("/")
    # A recipe is one directory: no empty name, nested path or "..".
    if not re.fullmatch(r"\w[\w.-]*", recipe_name):
        raise click.UsageError(
            f"'{agent_spec}' does not name an ADK recipe. {_BROWSE_RECIPES}"
        )

    urls = _find_recipe_urls(agent_spec, recipe_name)
    if len(urls) == 1:
        return urls[0]
    if not urls:
        raise click.UsageError(
            f"No ADK recipe is named '{recipe_name}'. adk-samples was renamed "
            f"to adk-recipes and not every sample was kept. {_BROWSE_RECIPES}"
        )
    candidates = "\n".join(f"  base_template: {url}" for url in urls)
    raise click.UsageError(
        f"'{agent_spec}' matches more than one ADK recipe. Pass one of:\n{candidates}"
    )


def _find_recipe_urls(agent_spec: str, recipe_name: str) -> list[str]:
    """URLs of every ``ADK_RECIPE_DIRS`` directory holding ``recipe_name``."""
    repo_spec = RemoteTemplateSpec(
        repo_url=ADK_RECIPES_REPO_URL, template_path="", git_ref="main"
    )
    try:
        # locked=True skips the version-lock check, which would otherwise
        # re-exec the CLI if the repo root carried a uv.lock.
        fetched = fetch_remote_template(repo_spec, locked=True)
    except RuntimeError as e:
        raise click.ClickException(
            f"Could not fetch {ADK_RECIPES_REPO_URL} to resolve '{agent_spec}': {e}\n"
            "Pass the recipe's GitHub URL instead."
        ) from e
    try:
        return [
            f"{ADK_RECIPES_REPO_URL}/tree/main/{recipe_dir}/{recipe_name}"
            for recipe_dir in ADK_RECIPE_DIRS
            if (fetched.repo_root / recipe_dir / recipe_name).is_dir()
        ]
    finally:
        shutil.rmtree(fetched.temp_dir, ignore_errors=True)
