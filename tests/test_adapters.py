"""CLI catalog/policy coverage, argument building, Headless guards, community parsing."""

from __future__ import annotations

import pytest
from conftest import posix_only

from obsidian_mcp import community
from obsidian_mcp.adapters import cli as cli_mod
from obsidian_mcp.adapters.bridge import BridgeAdapter
from obsidian_mcp.adapters.headless import HeadlessAdapter
from obsidian_mcp.errors import Code, ObsidianError
from obsidian_mcp.policy import CLI_RISK, Risk, cli_risk


def test_every_documented_cli_command_has_a_risk_class():
    names = {c["name"] for c in cli_mod.catalog()["commands"]}
    assert len(names) >= 100
    assert names == set(CLI_RISK), (names ^ set(CLI_RISK))


def test_flag_escalation():
    assert cli_risk("delete", {}, []) is Risk.TRASH
    assert cli_risk("delete", {}, ["permanent"]) is Risk.PERMANENT_DELETE
    assert cli_risk("task", {"ref": "a.md:1"}, []) is Risk.READ
    assert cli_risk("task", {"ref": "a.md:1"}, ["toggle"]) is Risk.WRITE
    assert cli_risk("eval", {"code": "1"}, []) is Risk.EVAL


def test_build_argv_validates_and_escapes():
    a = cli_mod.CLIAdapter("/bin/obsidian")
    argv = a.build_argv("create", {"name": "My Note", "content": "# T\n\tBody"}, ["open"], vault="My Vault")
    assert argv == ["/bin/obsidian", "vault=My Vault", "create", "name=My Note", "content=# T\\n\\tBody", "open"]
    with pytest.raises(ObsidianError):
        a.build_argv("create", {"bogus": 1})
    with pytest.raises(ObsidianError):
        a.build_argv("search", {})  # query is required
    with pytest.raises(ObsidianError):
        a.build_argv("search", {"query": "x", "format": "xml"})
    with pytest.raises(ObsidianError) as exc:
        a.build_argv("append", {"content": "literal \\n here"})
    assert exc.value.code == Code.UNSUPPORTED
    with pytest.raises(ObsidianError):
        a.build_argv("rm -rf", {})


def test_cli_unavailable_is_explained(service):
    with pytest.raises(ObsidianError) as exc:
        service.cli_run(None, "version")
    assert exc.value.code == Code.ADAPTER_UNAVAILABLE
    assert "1.12.7" in exc.value.hint


def test_cli_gates_before_running(service):
    with pytest.raises(ObsidianError) as exc:
        service.cli_run(None, "eval", {"code": "1+1"})
    assert exc.value.code == Code.PERMISSION_DENIED
    with pytest.raises(ObsidianError) as exc:
        service.cli_run(None, "plugin:install", {"id": "dataview"})
    assert exc.value.code == Code.PERMISSION_DENIED


@posix_only
def test_cli_runs_fake_binary(service, tmp_path):
    fake = tmp_path / "obsidian"
    fake.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    fake.chmod(0o755)
    service.config.cli_path = str(fake)
    out = service.cli_run(None, "search", {"query": "hello world", "limit": 5})
    assert out["output"].split("\n") == ["vault=Test", "search", "query=hello world", "limit=5"]
    fake.write_text("#!/bin/sh\necho 'Error: File not found'\n")
    with pytest.raises(ObsidianError) as exc:
        service.cli_run(None, "read", {"path": "x.md"})
    assert exc.value.code == Code.CLI_ERROR


@posix_only
def test_confirmation_for_eval(service, tmp_path):
    fake = tmp_path / "obsidian"
    fake.write_text("#!/bin/sh\necho 42\n")
    fake.chmod(0o755)
    service.config.cli_path = str(fake)
    service.config.permissions.eval_code = True
    pending = service.cli_run(None, "eval", {"code": "6*7"})
    assert pending["status"] == "confirmation_required"
    out = service.cli_run(None, "eval", {"code": "6*7"}, confirm_token=pending["confirm_token"])
    assert out["output"] == "42"


def test_bridge_must_be_localhost():
    with pytest.raises(ObsidianError):
        BridgeAdapter("http://example.com:27125", "t")
    with pytest.raises(ObsidianError) as exc:
        BridgeAdapter("http://127.0.0.1:1", None).call("GET", "/status")
    assert exc.value.code == Code.ADAPTER_UNAVAILABLE


@posix_only
def test_headless_refuses_credentials_and_unknown(tmp_path):
    fake = tmp_path / "ob"
    fake.write_text("#!/bin/sh\necho ok\n")
    fake.chmod(0o755)
    h = HeadlessAdapter(str(fake))
    with pytest.raises(ObsidianError) as exc:
        h.run("sync-setup", {"vault": "V", "password": "hunter2"})
    assert exc.value.code == Code.PERMISSION_DENIED
    with pytest.raises(ObsidianError):
        h.run("login")
    with pytest.raises(ObsidianError):
        h.run("sync", flags=["continuous"])
    assert h.run("sync-list-remote")["output"] == "ok"


@posix_only
def test_headless_publish_requires_permission_and_confirmation(service, tmp_path):
    fake = tmp_path / "ob"
    fake.write_text("#!/bin/sh\necho \"$@\"\n")
    fake.chmod(0o755)
    service.config.headless_path = str(fake)
    with pytest.raises(ObsidianError):
        service.headless_run(None, "publish")
    service.config.permissions.headless = True
    dry = service.headless_run(None, "publish", flags=["dry-run"])
    assert "--dry-run" in dry["output"]
    with pytest.raises(ObsidianError):
        service.headless_run(None, "publish")
    service.config.permissions.publish = True
    pending = service.headless_run(None, "publish")
    assert pending["status"] == "confirmation_required" and "--dry-run" in pending["preview"]
    done = service.headless_run(None, "publish", confirm_token=pending["confirm_token"])
    assert "--yes" in done["output"]


@posix_only
def test_headless_sync_conflict_guard(service, vault_dir, tmp_path):
    fake = tmp_path / "ob"
    fake.write_text("#!/bin/sh\necho synced\n")
    fake.chmod(0o755)
    service.config.headless_path = str(fake)
    service.config.permissions.headless = True
    service.config.permissions.sync_control = True
    (vault_dir / ".obsidian" / "core-plugins.json").write_text('{"sync": true}')
    with pytest.raises(ObsidianError) as exc:
        service.headless_run(None, "sync")
    assert "same device" in exc.value.message


def test_community_parsers():
    fields = community.inline_fields("Rating:: 5\n- task [due:: 2026-01-01]")
    assert {f["key"]: f["value"] for f in fields} == {"Rating": "5", "due": "2026-01-01"}
    meta = community.tasks_metadata("Do it 🔁 every week 📅 2026-02-01 🆔 abc ⛔ x1,y2")
    assert meta["recurrence"] == "every week" and meta["id"] == "abc" and meta["depends_on"] == ["x1", "y2"]
    code = community.dataview_eval_code('TABLE file.name FROM "x"')
    assert '"TABLE file.name FROM \\"x\\""' in code


def test_capabilities_report(service):
    caps = service.capabilities()
    assert caps["filesystem"]["available"]
    assert caps["cli"]["available"] is False and "1.12.7" in caps["cli"]["requires"]
    assert caps["permissions"]["eval_code"] is False


TYPED_WRAPPER_CALLS = [
    ("commands", {"filter": "x"}, []), ("search:context", {"query": "q", "path": "F", "limit": 20, "format": "json"}, ["case"]),
    ("base:query", {"path": "a.base", "view": "v", "format": "json"}, []),
    ("base:create", {"path": "a.base", "view": "v", "name": "n", "content": "c"}, []),
    ("history:read", {"path": "p", "version": 1}, []), ("sync:read", {"path": "p", "version": 1}, []),
    ("diff", {"path": "p", "from": 1, "to": 2, "filter": "local"}, []), ("history:restore", {"path": "p", "version": 1}, []),
    ("sync:restore", {"path": "p", "version": 1}, []), ("workspace", {}, ["ids"]), ("tabs", {}, ["ids"]),
    ("workspace:save", {"name": "n"}, []), ("open", {"path": "p"}, ["newtab"]), ("publish:add", {"path": "p"}, ["changed"]),
    ("publish:remove", {"path": "p"}, []), ("command", {"id": "x"}, []), ("eval", {"code": "1"}, []),
    ("move", {"path": "a", "to": "b"}, []), ("sync:status", {}, []), ("sync:deleted", {}, []), ("publish:site", {}, []),
    ("publish:list", {}, []), ("publish:status", {}, []), ("recents", {}, []), ("workspaces", {}, []),
]


@pytest.mark.parametrize(("command", "params", "flags"), TYPED_WRAPPER_CALLS)
def test_typed_wrappers_match_official_cli_docs(command, params, flags):
    cli_mod.CLIAdapter("/bin/obsidian").build_argv(command, params, flags)
