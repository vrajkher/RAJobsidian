"""Path isolation, content preservation, conflicts, backups, rollback, concurrency."""

from __future__ import annotations

import os
import threading

import pytest
from conftest import posix_only

from obsidian_mcp.errors import Code, ObsidianError


def code_of(exc: pytest.ExceptionInfo[ObsidianError]) -> Code:
    return exc.value.code


@pytest.mark.parametrize("bad", ["../escape.md", "a/../../x.md", "/etc/passwd", "C:/Windows/x", "a\x00b.md",
                                 "..\\x.md"])
def test_rejects_paths_outside_vault(service, bad):
    with pytest.raises(ObsidianError) as exc:
        service.read_note(None, bad)
    assert code_of(exc) in (Code.INVALID_PATH, Code.PATH_OUTSIDE_VAULT, Code.NOT_FOUND)
    with pytest.raises(ObsidianError):
        service.write_note(None, bad, "x")


@posix_only
def test_symlink_escape_is_blocked(service, vault_dir, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("top secret", encoding="utf-8")
    os.symlink(secret, vault_dir / "link.md")
    os.symlink(tmp_path, vault_dir / "outside")
    with pytest.raises(ObsidianError) as exc:
        service.read_note(None, "link.md")
    assert code_of(exc) == Code.PATH_OUTSIDE_VAULT
    with pytest.raises(ObsidianError):
        service.write_note(None, "outside/new.md", "x")
    listed = [i["path"] for i in service.list_files(None, limit=1000)["items"]]
    assert "link.md" not in listed and not any(p.startswith("outside/") for p in listed)


def test_hidden_config_is_not_content(service):
    with pytest.raises(ObsidianError) as exc:
        service.write_note(None, ".obsidian/app.json", "{}")
    assert code_of(exc) == Code.INVALID_PATH
    assert all(not i["path"].startswith(".") for i in service.list_files(None, limit=1000)["items"])


def test_unicode_and_crlf_preserved(service, vault_dir):
    note = service.read_note(None, "Unicode/ગુજરાતી.md")
    assert "नमस्ते" in note["content"]
    service.append_note(None, "Unicode/ગુજરાતી.md", "નવી લાઇન")
    raw = (vault_dir / "Unicode/ગુજરાતી.md").read_bytes().decode("utf-8")
    assert raw.endswith("નવી લાઇન") and "नमस्ते" in raw
    service.append_note(None, "CRLF.md", "line three\nline four")
    data = (vault_dir / "CRLF.md").read_bytes()
    assert data == b"line one\r\nline two\r\nline three\r\nline four"


def test_binary_attachment_roundtrip(service, vault_dir):
    blob = service.read_attachment(None, "pic.png")
    assert blob["mime"] == "image/png"
    out = service.write_attachment(None, "copy.png", blob["base64"])
    assert (vault_dir / "copy.png").read_bytes() == (vault_dir / "pic.png").read_bytes()
    assert out["embed"] == "![[copy.png]]"
    with pytest.raises(ObsidianError) as exc:
        service.write_attachment(None, "bad.png", "not base64!!")
    assert code_of(exc) == Code.INVALID_ARGUMENT


def test_etag_conflict_detected(service):
    first = service.read_note(None, "Ideas.md")
    service.append_note(None, "Ideas.md", "someone else edited")
    with pytest.raises(ObsidianError) as exc:
        service.write_note(None, "Ideas.md", "mine", overwrite=True, if_match=first["etag"])
    assert code_of(exc) == Code.CONFLICT


def test_create_refuses_overwrite_without_flag(service):
    with pytest.raises(ObsidianError) as exc:
        service.write_note(None, "Ideas.md", "x")
    assert code_of(exc) == Code.ALREADY_EXISTS


def test_backup_and_rollback(service, vault_dir):
    before = (vault_dir / "Ideas.md").read_text(encoding="utf-8")
    res = service.write_note(None, "Ideas.md", "replaced", overwrite=True)
    assert res["backup_id"]
    service.rollback(None, res["operation_id"])
    assert (vault_dir / "Ideas.md").read_text(encoding="utf-8") == before


def test_rollback_refuses_after_later_edit(service, vault_dir):
    res = service.write_note(None, "Ideas.md", "v2", overwrite=True)
    service.write_note(None, "Ideas.md", "v3", overwrite=True)
    out = service.rollback(None, res["operation_id"])
    assert out["results"][0]["status"] == "conflict"
    assert (vault_dir / "Ideas.md").read_text(encoding="utf-8") == "v3"


def test_trash_restore_and_permanent_delete_flow(service, vault_dir):
    res = service.trash_file(None, "Orphan.md")
    assert not (vault_dir / "Orphan.md").exists()
    assert any(i["original_path"] == "Orphan.md" for i in service.list_trash(None)["items"])
    service.restore_from_trash(None, res["trash_path"])
    assert (vault_dir / "Orphan.md").exists()
    # Permanent delete: off by default, then needs a confirmation token.
    with pytest.raises(ObsidianError) as exc:
        service.delete_permanently(None, "Orphan.md")
    assert code_of(exc) == Code.PERMISSION_DENIED
    service.config.permissions.permanent_delete = True
    pending = service.delete_permanently(None, "Orphan.md")
    assert pending["status"] == "confirmation_required"
    with pytest.raises(ObsidianError):
        service.delete_permanently(None, "Ideas.md", confirm_token=pending["confirm_token"])
    pending = service.delete_permanently(None, "Orphan.md")
    service.delete_permanently(None, "Orphan.md", confirm_token=pending["confirm_token"])
    assert not (vault_dir / "Orphan.md").exists()


def test_read_only_mode(service):
    service.config.permissions.write = False
    with pytest.raises(ObsidianError) as exc:
        service.append_note(None, "Ideas.md", "x")
    assert code_of(exc) == Code.PERMISSION_DENIED
    assert service.append_note(None, "Ideas.md", "x", dry_run=True)["dry_run"]


def test_concurrent_conditional_writes_one_wins(service):
    etag = service.read_note(None, "Ideas.md")["etag"]
    outcomes = []

    def worker(i):
        try:
            service.write_note(None, "Ideas.md", f"writer {i}", overwrite=True, if_match=etag)
            outcomes.append("ok")
        except ObsidianError as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 1
    assert outcomes.count(Code.CONFLICT) == 7


def test_batch_atomic_rolls_back(service, vault_dir):
    before = (vault_dir / "Ideas.md").read_text(encoding="utf-8")
    out = service.batch(None, [
        {"action": "append_note", "path": "Ideas.md", "content": "batched"},
        {"action": "patch_note", "path": "Ideas.md", "ops": [{"op": "replace", "find": "NOPE", "replace": "x"}]},
    ], dry_run=False, atomic=True)
    assert out["status"] == "failed" and out["rolled_back"]
    assert (vault_dir / "Ideas.md").read_text(encoding="utf-8") == before


def test_batch_preview_changes_nothing(service, vault_dir):
    before = (vault_dir / "Ideas.md").read_text(encoding="utf-8")
    out = service.batch(None, [{"action": "append_note", "path": "Ideas.md", "content": "x"}])
    assert out["dry_run"] and "+x" in out["results"][0]["result"]["diff"]
    assert (vault_dir / "Ideas.md").read_text(encoding="utf-8") == before
