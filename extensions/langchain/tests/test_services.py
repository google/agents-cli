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

"""Unit tests for LangGraph persistence services (Cloud SQL & in-memory fallback)."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

_EXTENSION_DIR = next(
    p / "extensions" / "langchain" / "template"
    for p in Path(__file__).resolve().parents
    if (p / "extensions" / "langchain" / "template").is_dir()
)
_SERVICES_PATH = _EXTENSION_DIR / "app/app_utils/services.py"

_DB_VARS = (
    "SESSION_DB_URI",
    "INSTANCE_CONNECTION_NAME",
    "DB_PASS",
    "DB_USER",
    "DB_NAME",
    "CLOUD_SQL_SOCKET_DIR",
)


def _load_services():
    spec = importlib.util.spec_from_file_location("lg_services", str(_SERVICES_PATH))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["lg_services"] = mod
    spec.loader.exec_module(mod)
    return mod


def _clear_db_env(monkeypatch) -> None:
    for var in _DB_VARS:
        monkeypatch.delenv(var, raising=False)


class TestSessionDbUri:
    def test_returns_none_when_unset(self, monkeypatch):
        _clear_db_env(monkeypatch)
        assert _load_services()._session_db_uri() is None

    def test_prefers_explicit_session_db_uri(self, monkeypatch):
        _clear_db_env(monkeypatch)
        monkeypatch.setenv("SESSION_DB_URI", "postgresql://u:p@localhost:5432/db")
        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "proj:reg:inst")
        monkeypatch.setenv("DB_PASS", "ignored")
        assert _load_services()._session_db_uri() == "postgresql://u:p@localhost:5432/db"

    def test_builds_cloud_sql_socket_uri_and_escapes_credentials(self, monkeypatch):
        _clear_db_env(monkeypatch)
        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "my-proj:us-east1:my-db")
        monkeypatch.setenv("DB_PASS", "p@ss:w/rd#1")
        monkeypatch.setenv("DB_USER", "my/user")
        monkeypatch.setenv("DB_NAME", "agent/db")
        uri = _load_services()._session_db_uri()
        assert uri == (
            "postgresql://my%2Fuser:p%40ss%3Aw%2Frd%231@/agent%2Fdb"
            "?host=/cloudsql/my-proj:us-east1:my-db"
        )

    def test_empty_user_and_db_name_fall_back_to_postgres(self, monkeypatch):
        _clear_db_env(monkeypatch)
        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "my-proj:us-east1:my-db")
        monkeypatch.setenv("DB_PASS", "secret")
        monkeypatch.setenv("DB_USER", "")
        monkeypatch.setenv("DB_NAME", "")
        uri = _load_services()._session_db_uri()
        assert (
            uri
            == "postgresql://postgres:secret@/postgres?host=/cloudsql/my-proj:us-east1:my-db"
        )

    def test_strips_trailing_slash_from_cloud_sql_socket_dir(self, monkeypatch):
        _clear_db_env(monkeypatch)
        monkeypatch.setenv("INSTANCE_CONNECTION_NAME", "my-proj:us-east1:my-db")
        monkeypatch.setenv("DB_PASS", "secret")
        monkeypatch.setenv("CLOUD_SQL_SOCKET_DIR", "/custom/cloudsql/")
        uri = _load_services()._session_db_uri()
        assert (
            uri
            == "postgresql://postgres:secret@/postgres?host=/custom/cloudsql/my-proj:us-east1:my-db"
        )


class TestPersistenceContext:
    def test_falls_back_to_in_memory_when_db_unconfigured(self, monkeypatch):
        _clear_db_env(monkeypatch)

        class DummyInMemorySaver:
            pass

        class DummyInMemoryStore:
            pass

        lg_mod = types.ModuleType("langgraph")
        cp_mod = types.ModuleType("langgraph.checkpoint")
        cp_mem_mod = types.ModuleType("langgraph.checkpoint.memory")
        cp_mem_mod.InMemorySaver = DummyInMemorySaver  # type: ignore[attr-defined]
        st_mod = types.ModuleType("langgraph.store")
        st_mem_mod = types.ModuleType("langgraph.store.memory")
        st_mem_mod.InMemoryStore = DummyInMemoryStore  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "langgraph", lg_mod)
        monkeypatch.setitem(sys.modules, "langgraph.checkpoint", cp_mod)
        monkeypatch.setitem(sys.modules, "langgraph.checkpoint.memory", cp_mem_mod)
        monkeypatch.setitem(sys.modules, "langgraph.store", st_mod)
        monkeypatch.setitem(sys.modules, "langgraph.store.memory", st_mem_mod)

        mod = _load_services()

        async def _run():
            async with mod.persistence_context() as services:
                assert isinstance(services.checkpointer, DummyInMemorySaver)
                assert isinstance(services.store, DummyInMemoryStore)
                assert services.has_postgres is False

        asyncio.run(_run())

    def test_initialises_postgres_checkpointer_and_store_on_shared_pool(
        self, monkeypatch
    ):
        _clear_db_env(monkeypatch)
        monkeypatch.setenv("SESSION_DB_URI", "postgresql://u:p@localhost:5432/db")

        events: list[tuple[str, object]] = []

        class FakePool:
            def __init__(self, conninfo, min_size, max_size, kwargs, open):
                self.conninfo = conninfo
                self.min_size = min_size
                self.max_size = max_size
                self.kwargs = kwargs
                self.open_arg = open
                events.append(("pool_init", conninfo))

            async def open(self):
                events.append(("pool_open", None))

            async def close(self):
                events.append(("pool_close", None))

        class FakePostgresSaver:
            def __init__(self, pool):
                self.pool = pool
                events.append(("saver_init", pool))

            async def setup(self):
                events.append(("saver_setup", None))

        class FakePostgresStore:
            def __init__(self, pool):
                self.pool = pool
                events.append(("store_init", pool))

            async def setup(self):
                events.append(("store_setup", None))

        pool_mod = types.ModuleType("psycopg_pool")
        pool_mod.AsyncConnectionPool = FakePool  # type: ignore[attr-defined]
        cp_aio_mod = types.ModuleType("langgraph.checkpoint.postgres.aio")
        cp_aio_mod.AsyncPostgresSaver = FakePostgresSaver  # type: ignore[attr-defined]
        st_aio_mod = types.ModuleType("langgraph.store.postgres.aio")
        st_aio_mod.AsyncPostgresStore = FakePostgresStore  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
        monkeypatch.setitem(sys.modules, "langgraph.checkpoint.postgres.aio", cp_aio_mod)
        monkeypatch.setitem(sys.modules, "langgraph.store.postgres.aio", st_aio_mod)

        mod = _load_services()

        async def _run():
            async with mod.persistence_context() as services:
                assert isinstance(services.checkpointer, FakePostgresSaver)
                assert isinstance(services.store, FakePostgresStore)
                assert services.has_postgres is True
                assert services.checkpointer.pool is services.store.pool
                assert services.checkpointer.pool.kwargs == {
                    "autocommit": True,
                    "prepare_threshold": 0,
                }

        asyncio.run(_run())
        assert [name for name, _ in events] == [
            "pool_init",
            "pool_open",
            "saver_init",
            "saver_setup",
            "store_init",
            "store_setup",
            "pool_close",
        ]


class TestA2AThreadAndLifespanWiring:
    def test_executor_forwards_context_id_as_thread_id(self, monkeypatch):
        a2a_path = _EXTENSION_DIR / "app/app_utils/a2a.py"
        content_path = _EXTENSION_DIR / "app/app_utils/content.py"

        content_spec = importlib.util.spec_from_file_location(
            "app.app_utils.content", str(content_path)
        )
        assert content_spec is not None and content_spec.loader is not None
        content_mod = importlib.util.module_from_spec(content_spec)
        content_spec.loader.exec_module(content_mod)

        seen_configs: list[dict] = []

        class FakeAgent:
            async def astream(self, state, *, config=None, stream_mode=None):
                seen_configs.append(config)
                yield ("values", {"messages": [types.SimpleNamespace(content="hello")]})

        app_mod = types.ModuleType("app")
        agent_mod = types.ModuleType("app.agent")
        agent_mod.root_agent = FakeAgent()  # type: ignore[attr-defined]
        app_utils_mod = types.ModuleType("app.app_utils")
        routes_mod = types.ModuleType("a2a.server.routes")
        routes_mod.add_a2a_routes_to_fastapi = lambda *a, **k: None  # type: ignore[attr-defined]
        routes_mod.create_agent_card_routes = lambda *a, **k: None  # type: ignore[attr-defined]
        routes_mod.create_jsonrpc_routes = lambda *a, **k: None  # type: ignore[attr-defined]
        fastapi_mod = types.ModuleType("fastapi")
        fastapi_mod.FastAPI = object  # type: ignore[attr-defined]
        otel_mod = types.ModuleType("opentelemetry")
        otel_mod.trace = types.SimpleNamespace(  # type: ignore[attr-defined]
            get_tracer=lambda _: types.SimpleNamespace(
                start_as_current_span=lambda _: nullcontext()
            )
        )

        monkeypatch.setitem(sys.modules, "app", app_mod)
        monkeypatch.setitem(sys.modules, "app.agent", agent_mod)
        monkeypatch.setitem(sys.modules, "app.app_utils", app_utils_mod)
        monkeypatch.setitem(sys.modules, "app.app_utils.content", content_mod)
        monkeypatch.setitem(sys.modules, "a2a.server.routes", routes_mod)
        monkeypatch.setitem(sys.modules, "fastapi", fastapi_mod)
        monkeypatch.setitem(sys.modules, "opentelemetry", otel_mod)

        a2a_spec = importlib.util.spec_from_file_location(
            "app.app_utils.a2a", str(a2a_path)
        )
        assert a2a_spec is not None and a2a_spec.loader is not None
        a2a_mod = importlib.util.module_from_spec(a2a_spec)
        a2a_spec.loader.exec_module(a2a_mod)

        class FakeEventQueue:
            async def enqueue_event(self, event):
                pass

        executor = a2a_mod.LangGraphAgentExecutor(agent=FakeAgent())
        fake_task = types.SimpleNamespace(id="task-1", context_id="ctx-thread-42")
        fake_ctx = types.SimpleNamespace(
            get_user_input=lambda: "hi",
            current_task=fake_task,
        )

        asyncio.run(executor.execute(fake_ctx, FakeEventQueue()))
        assert seen_configs == [{"configurable": {"thread_id": "ctx-thread-42"}}]

        seen_configs.clear()
        unthreaded_task = types.SimpleNamespace(id="task-99", context_id=None)
        unthreaded_ctx = types.SimpleNamespace(
            get_user_input=lambda: "hi",
            current_task=unthreaded_task,
        )
        asyncio.run(executor.execute(unthreaded_ctx, FakeEventQueue()))
        assert seen_configs == [{"configurable": {"thread_id": "task-99"}}]

    def test_lifespan_preserves_custom_store_when_fallback_active(self, monkeypatch):
        fast_api_path = _EXTENSION_DIR / "app/fast_api_app.py"
        updates: list[dict] = []
        mounted: list[object] = []

        class FakeGraph:
            checkpointer = None
            store = "user-custom-store"

            def copy(self, update=None):
                updates.append(update)
                return ("copied-graph", update)

        class FakeFastAPI:
            def __init__(self, **kwargs):
                self.state = types.SimpleNamespace()

            def get(self, _path):
                return lambda fn: fn

        @asynccontextmanager
        async def fake_persistence_context():
            yield types.SimpleNamespace(
                checkpointer="fallback-cp",
                store="fallback-store",
                has_postgres=False,
            )

        app_mod = types.ModuleType("app")
        agent_mod = types.ModuleType("app.agent")
        agent_mod.root_agent = FakeGraph()  # type: ignore[attr-defined]
        app_utils_mod = types.ModuleType("app.app_utils")
        a2a_mod = types.ModuleType("app.app_utils.a2a")
        a2a_mod.add_a2a_routes = lambda _app, agent=None: mounted.append(agent)  # type: ignore[attr-defined]
        services_mod = types.ModuleType("app.app_utils.services")
        services_mod.persistence_context = fake_persistence_context  # type: ignore[attr-defined]
        telemetry_mod = types.ModuleType("app.app_utils.telemetry")
        telemetry_mod.setup_telemetry = lambda: None  # type: ignore[attr-defined]
        fastapi_mod = types.ModuleType("fastapi")
        fastapi_mod.FastAPI = FakeFastAPI  # type: ignore[attr-defined]

        monkeypatch.setitem(sys.modules, "app", app_mod)
        monkeypatch.setitem(sys.modules, "app.agent", agent_mod)
        monkeypatch.setitem(sys.modules, "app.app_utils", app_utils_mod)
        monkeypatch.setitem(sys.modules, "app.app_utils.a2a", a2a_mod)
        monkeypatch.setitem(sys.modules, "app.app_utils.services", services_mod)
        monkeypatch.setitem(sys.modules, "app.app_utils.telemetry", telemetry_mod)
        monkeypatch.setitem(sys.modules, "fastapi", fastapi_mod)

        spec = importlib.util.spec_from_file_location(
            "app.fast_api_app", str(fast_api_path)
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        async def _run():
            async with mod.lifespan(mod.app):
                pass

        asyncio.run(_run())
        assert updates == [{"checkpointer": "fallback-cp", "store": "user-custom-store"}]
        assert mounted == [
            (
                "copied-graph",
                {"checkpointer": "fallback-cp", "store": "user-custom-store"},
            )
        ]
