import os

import pytest

from room import cli
from room.memory.vault import MANIFEST, clear_vault, default_vault, export_vault

from .conftest import add
from .test_vault import EDGES, PEOPLE


def purge(*extra):
    return cli.main(["memory", "purge", *extra])


@pytest.fixture
def exported(mem, isolated_home):
    """Memory with notes, exported to the default vault; the store is closed before purging."""
    add(mem, PEOPLE, EDGES)
    export_vault()
    mem.close()
    return default_vault()


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() or p.is_symlink())


def test_default_vault_is_under_room_home_or_configured(isolated_home, monkeypatch, tmp_path):
    assert default_vault() == isolated_home / "vault"
    monkeypatch.setenv("MEMORY_VAULT", str(tmp_path / "elsewhere"))
    assert default_vault() == tmp_path / "elsewhere"


def test_purge_removes_managed_files_and_empty_folders(exported, capsys):
    assert len(_files(exported)) == 6
    assert purge("--yes") == 0
    out = capsys.readouterr().out
    assert "Vault: removed 6 file(s)" in out and "Memory purged." in out
    assert not exported.exists()


def test_user_files_survive_and_keep_the_folder(exported, capsys):
    (exported / "my-notes.md").write_text("mine")
    (exported / "projects" / "my-project-idea.md").write_text("mine too")
    (exported / "inbox").mkdir()
    (exported / "inbox" / "todo.md").write_text("keep")
    assert purge("--yes") == 0
    assert "folder kept: it holds files you added" in capsys.readouterr().out
    assert _files(exported) == ["inbox/todo.md", "my-notes.md", "projects/my-project-idea.md"]
    assert not (exported / "people").exists() and not (exported / MANIFEST).exists()


def test_no_vault_purge_behaves_as_before(mem, isolated_home, monkeypatch, capsys):
    add(mem, PEOPLE)
    mem.close()
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "purge")
    assert purge() == 0
    out = capsys.readouterr().out
    assert "Vault:" not in out and "vault copy" not in prompts[0]
    assert out.strip() == "Memory purged."


def test_nothing_at_all_still_says_no_memory(capsys):
    assert purge("--yes") == 0
    assert "No memory to purge." in capsys.readouterr().out


def test_vault_without_manifest_is_not_touched(isolated_home, capsys):
    vault = default_vault()
    (vault / "people").mkdir(parents=True)
    (vault / "index.md").write_text("the user's own vault index")
    (vault / "people" / "me-1.md").write_text("theirs")
    assert purge("--yes") == 0
    assert "No memory to purge." in capsys.readouterr().out
    assert _files(vault) == ["index.md", "people/me-1.md"]


def test_vault_only_purge_still_runs(exported, isolated_home, capsys):
    """Memory already deleted by hand, but the vault copy remains: purge clears it."""
    for p in isolated_home.glob("memory.db*"):
        p.unlink()
    assert purge("--yes") == 0
    assert "Vault: removed 6 file(s)" in capsys.readouterr().out


def test_confirmation_mentions_the_vault(exported, monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "no")
    assert purge() == 1
    assert f"The exported vault copy in {exported} is cleared too" in prompts[0]
    assert "your own files there are kept" in prompts[0]
    assert len(_files(exported)) == 6  # aborted: nothing removed


def test_symlinks_never_lead_outside_the_vault(exported, tmp_path):
    outside = tmp_path / "outside"
    (outside / "people").mkdir(parents=True)
    victim_file = outside / "victim.md"
    victim_file.write_text("do not delete")
    people_note = next((exported / "people").glob("me-*.md"))
    folder_victim = outside / "people" / people_note.name
    folder_victim.write_text("do not delete either")

    # A managed note replaced by a symlink to a file outside.
    project_note = next((exported / "projects").glob("agent-room-*.md"))
    project_note.unlink()
    project_note.symlink_to(victim_file)
    # A managed folder replaced by a symlink to a folder outside with a same-named file.
    for p in (exported / "people").iterdir():
        p.unlink()
    (exported / "people").rmdir()
    (exported / "people").symlink_to(outside / "people", target_is_directory=True)
    # A manifest entry that tries to escape.
    manifest = exported / MANIFEST
    manifest.write_text(manifest.read_text().replace('"files": [', '"files": ["../../outside/victim.md", '))

    res = clear_vault(exported)
    assert victim_file.read_text() == "do not delete"
    assert folder_victim.read_text() == "do not delete either"
    assert project_note.is_symlink() and (exported / "people").is_symlink()  # left for the user
    assert not any(r.startswith("..") for r in res.removed)
    assert not res.vault_deleted


def test_symlinked_vault_path_is_not_followed(mem, tmp_path, monkeypatch, capsys):
    real = tmp_path / "real-vault"
    add(mem, PEOPLE)
    export_vault(real)
    mem.close()
    link = tmp_path / "linked-vault"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("MEMORY_VAULT", str(link))
    res = clear_vault()
    assert res.removed == [] and "symlink" in res.skipped_reason
    assert (real / MANIFEST).exists()
    assert purge("--yes") == 0
    assert (real / MANIFEST).exists() and "Vault" not in capsys.readouterr().out


def test_configured_vault_is_cleared(mem, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MEMORY_VAULT", str(tmp_path / "my-vault"))
    add(mem, PEOPLE)
    assert cli.main(["memory", "export"]) == 0
    mem.close()
    assert (tmp_path / "my-vault" / "index.md").exists()
    assert purge("--yes") == 0
    assert not (tmp_path / "my-vault").exists()


def test_forget_does_not_touch_the_vault(exported, capsys):
    from room.memory.store import MemoryStore

    with MemoryStore() as store:
        me = store.find_nodes("Me")[0].id
    assert cli.main(["memory", "forget", str(me)]) == 0
    assert list((exported / "people").glob("me-*.md"))  # still there until the next export
    assert cli.main(["memory", "export"]) == 0
    assert not (exported / "people").exists()
