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

"""Subprocess helpers for agents CLI."""

import contextlib
import io
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import click

from google.agents.cli import _tools

# Set for an extension's command so any re-entry into `agents-cli <cmd>` runs the
# built-in instead of recursing back into the override.
DISABLE_OVERRIDES_ENV = "AGENTS_CLI_DISABLE_OVERRIDES"
# Exported so an extension's scripts can find sibling resources (templates etc.).
EXTENSION_DIR_ENV = "AGENTS_CLI_EXTENSION_DIR"


def redact_cmd(args: list[str]) -> str:
    """Mask sensitive information in command arguments and return joined string.

    Masks arguments like --github-pat and environment variables containing secrets.
    """
    redacted_cmd_list = list(args)
    for i, arg in enumerate(args):
        if arg == "--github-pat" and i + 1 < len(args):
            redacted_cmd_list[i + 1] = "[REDACTED]"
        elif any(
            secret in arg
            for secret in ["GITHUB_PAT", "GH_TOKEN", "GITHUB_TOKEN", "GITHUB_APP_KEY"]
        ):
            redacted_cmd_list[i] = "[REDACTED]"

    return shlex.join(redacted_cmd_list)


def run(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict | None = None,
    capture: bool = False,
    print_cmd: bool = True,
    check: bool = True,
    check_err_msg: str | None = None,
    input_data: bytes | None = None,
    timeout: int | None = None,
    resolve_executable: bool = True,
) -> subprocess.CompletedProcess:
    """Run a subprocess, streaming output by default.

    Args:
        args: Command and arguments.
        cwd: Working directory for the subprocess.
        env: Extra environment variables. Merged with os.environ if provided.
        capture: If True, capture stdout/stderr instead of streaming.
            Defaults to False.
        print_cmd: If True, print the command before executing.
            Defaults to True.
        check: If True, raise ClickException on non-zero exit.
            Defaults to True.
        check_err_msg: Error message prefix for check failures.
        input_data: Bytes to feed to stdin of the subprocess.
        timeout: Timeout in seconds for the subprocess.
        resolve_executable: If True, resolve the executable path using require_tool.
            Defaults to True.

    Returns:
        CompletedProcess instance.
    """
    cmd_str = redact_cmd(args)

    if print_cmd:
        click.secho(f"  ▸ {cmd_str}", fg="cyan", dim=True)

    run_env = None
    if env is not None:
        run_env = {**os.environ, **env}

    # Capture output as UTF-8 text (replacing undecodable bytes) unless we're
    # piping raw bytes to stdin, where child output must stay bytes. Without an
    # explicit encoding, subprocess uses the locale codec + strict errors, which
    # raises UnicodeDecodeError on non-UTF-8 locales (e.g. cp1252 on Windows).
    text_mode = input_data is None
    text_kwargs = {"encoding": "utf-8", "errors": "replace"} if text_mode else {}

    if capture:
        result = run_resolved(
            args,
            resolve_executable=resolve_executable,
            capture_output=True,
            text=text_mode,
            cwd=str(cwd) if cwd else None,
            input=input_data,
            env=run_env,
            timeout=timeout,
            **text_kwargs,
        )
    else:
        # Under click.testing.CliRunner, sys.stdout is a StringIO-like object
        # that doesn't have a fileno(). Subprocess on Windows requires a fileno.
        # We check carefully to avoid issues with mocks or unusual environments.
        use_fallback = True
        if hasattr(sys.stdout, "fileno") and callable(sys.stdout.fileno):
            try:
                sys.stdout.fileno()
                use_fallback = False
            except (io.UnsupportedOperation, AttributeError, OSError):
                pass

        if not use_fallback:
            result = run_resolved(
                args,
                resolve_executable=resolve_executable,
                stdout=sys.stdout,
                stderr=sys.stderr,
                cwd=str(cwd) if cwd else None,
                input=input_data,
                env=run_env,
                timeout=timeout,
            )
        else:
            # Fallback to capture and manual emit for environments without a
            # real stdout fileno (e.g. click.testing.CliRunner).
            result = run_resolved(
                args,
                resolve_executable=resolve_executable,
                capture_output=True,
                text=text_mode,
                cwd=str(cwd) if cwd else None,
                input=input_data,
                env=run_env,
                timeout=timeout,
                **text_kwargs,
            )
            # stdout/stderr are bytes when input_data is set (text=False).
            for stream, content in (
                (sys.stdout, result.stdout),
                (sys.stderr, result.stderr),
            ):
                if not content:
                    continue
                if isinstance(content, bytes):
                    content = content.decode(errors="replace")
                stream.write(content)

    if check and result.returncode != 0:
        error_msg = check_err_msg or f"Command failed: {cmd_str}"
        # When output was captured it never reached the console, so fold it into
        # the error — otherwise the failure reason is lost. Streamed runs already
        # printed it. stdout/stderr are bytes when input_data is set (text=False).
        detail = ""
        if capture:
            captured = "\n".join(
                part.strip()
                for part in (result.stdout, result.stderr)
                if isinstance(part, str) and part.strip()
            )
            if captured:
                detail = f"\n{captured}"
        raise click.ClickException(f"{error_msg} (exit code {result.returncode}){detail}")

    return result


def _looks_like_path(token: str) -> bool:
    """A `run:` token the author wrote as a path, e.g. `scripts/run.py`."""
    return "/" in token or os.sep in token


def run_extension_command(
    run_vector: Sequence[str],
    extra_args: list[str],
    *,
    cwd: Path,
    extension_root: Path,
) -> int:
    """Run an extension's declared `run:` vector with the user's argv appended.

    Differs from `run` in ways an override needs: the vector is extension-supplied
    and passed through verbatim (no `require_tool` resolution), the command is
    not echoed, and the child's exit code is returned rather than raised, so the
    override exits like the command it replaced.

    Tokens that exist as paths under `extension_root` are made absolute, so a
    vendored script is found regardless of cwd.
    """
    # Only tokens written as paths. Resolving every token would rewrite an
    # ordinary argument that happens to name a file in the extension, e.g.
    # `run: ["make", "build"]` in an extension that also ships `build/`.
    resolved_vector = [
        str((extension_root / tok).resolve())
        if _looks_like_path(tok) and (extension_root / tok).exists()
        else tok
        for tok in run_vector
    ]
    result = run(
        resolved_vector + list(extra_args),
        cwd=cwd,
        env={DISABLE_OVERRIDES_ENV: "1", EXTENSION_DIR_ENV: str(extension_root)},
        print_cmd=False,
        check=False,
        resolve_executable=False,
    )
    return result.returncode


@contextlib.contextmanager
def _wrap_launch_errors(executable: str | None, args: list[str]):
    """Turn a resolved tool's launch-time OSError into a ToolNotExecutableError.

    On Windows, shutil.which doesn't check ACLs or security policies, so a
    blocked tool resolves fine and only fails at launch, even with check=False.
    Only ERROR_ACCESS_DENIED maps to PermissionError; policy blocks (e.g.
    WinError 4551 from Application Control) surface as a plain OSError.
    FileNotFoundError and unresolved executables (``executable is None``) keep
    the raw error so callers' own handling still applies.
    """
    try:
        yield
    except FileNotFoundError:
        raise
    except (PermissionError, OSError) as e:
        if executable is None:
            raise
        msg = f"'{executable}' was found at '{args[0]}' but could not be executed: {e}"
        if _tools.is_windows():
            msg += "\n  It may be blocked by a security policy (e.g. AppLocker, WDAC, Defender)."
        raise _tools.ToolNotExecutableError(msg) from e


def run_resolved(
    args: list[str], *, resolve_executable: bool = True, **kwargs
) -> subprocess.CompletedProcess:
    """Wrapper around subprocess.run with optional executable resolution.

    Args:
        args: Command and arguments as a list of strings.
        resolve_executable: If True, resolve the executable path using require_tool.
            Defaults to True.
        **kwargs: Additional keyword arguments passed to subprocess.run.

    Raises:
        ToolNotFoundError: If resolve_executable is True and the tool cannot be found.
        ToolNotExecutableError: If resolve_executable is True and the OS refuses
            to execute the resolved tool.

    Returns:
        CompletedProcess instance.
    """
    if isinstance(args, str):
        raise ValueError("args must be a list of strings, not a single string.")

    executable = None
    if resolve_executable and args:
        executable = args[0]
        # Create a shallow copy to avoid modifying the original list passed by reference
        args = args.copy()
        args[0] = _tools.require_tool(executable)

    with _wrap_launch_errors(executable, args):
        return subprocess.run(args, **kwargs)


def popen_resolved(
    args: list[str], *, resolve_executable: bool = True, **kwargs
) -> subprocess.Popen:
    """Wrapper around subprocess.Popen with optional executable resolution.

    Args:
        args: Command and arguments as a list of strings.
        resolve_executable: If True, resolve the executable path using require_tool.
            Defaults to True.
        **kwargs: Additional keyword arguments passed to subprocess.Popen.

    Raises:
        ToolNotFoundError: If resolve_executable is True and the tool cannot be found.
        ToolNotExecutableError: If resolve_executable is True and the OS refuses
            to execute the resolved tool.

    Returns:
        Popen instance.
    """
    if isinstance(args, str):
        raise ValueError("args must be a list of strings, not a single string.")

    executable = None
    if resolve_executable and args:
        executable = args[0]
        # Create a shallow copy to avoid modifying the original list passed by reference
        args = args.copy()
        args[0] = _tools.require_tool(executable)

    with _wrap_launch_errors(executable, args):
        return subprocess.Popen(args, **kwargs)


def popen_resolved_detached(
    args: list[str], *, resolve_executable: bool = True, **kwargs
) -> subprocess.Popen:
    """Wrapper around subprocess.Popen for launching detached background processes.

    Handles cross-platform differences for process detaching:
    - On POSIX, sets start_new_session=True.
    - On Windows, sets creationflags to DETACHED_PROCESS and CREATE_NEW_PROCESS_GROUP.
    - Ensures stdin is redirected to subprocess.DEVNULL.

    Args:
        args: Command and arguments as a list of strings.
        resolve_executable: If True, resolve the executable path using require_tool.
            Defaults to True.
        **kwargs: Additional keyword arguments passed to subprocess.Popen.

    Raises:
        ToolNotFoundError: If resolve_executable is True and the tool cannot be found.
        ToolNotExecutableError: If resolve_executable is True and the OS refuses
            to execute the resolved tool.

    Returns:
        Popen instance.
    """
    stdin = kwargs.get("stdin")
    if stdin is not None and stdin is not subprocess.DEVNULL:
        raise ValueError("popen_resolved_detached only supports stdin=DEVNULL.")
    kwargs["stdin"] = subprocess.DEVNULL

    if os.name == "nt":
        # Windows-specific process creation flags for detaching
        create_new_process_group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        flags = create_new_process_group | create_no_window
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flags
    else:
        # POSIX way of detaching
        kwargs["start_new_session"] = True

    return popen_resolved(args, resolve_executable=resolve_executable, **kwargs)
