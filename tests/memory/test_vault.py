import json
import stat

import pytest

from room import cli
from room.memory import vault as vault_mod
from room.memory.paths import project_root
from room.memory.vault import MANIFEST, VaultSafetyError, export_vault, slugify

from .conftest import add

# Fake key, assembled at runtime so the repo never holds a key-shaped literal.
FAKE_GOOGLE_KEY = "AI" "za" + "SyD" + "x7Kq2LmN9pRt4VwZ8bC1eF3gH5jJ6"

PEOPLE = [
    {"type": "person", "name": "Me", "summary": "Applying for a CS masters.", "confidence": 0.9},
    {"type": "project", "name": "Agent-Aware Power Manager", "summary": "Keeps the Mac awake while agents work.", "confidence": 0.8},
    {"type": "project", "name": "Agent Room", "summary": "Local multi-agent room.", "confidence": 0.9},
    {"type": "decision", "name": "Target school", "summary": "Apply to Northfield first.", "confidence": 0.7},
]
EDGES = [
    {"src": "Me", "relation": "works_on", "dst": "Agent Room"},
    {"src": "Agent-Aware Power Manager", "relation": "part_of", "dst": "Agent Room"},
]


def _files(vault):
    return sorted(str(p.relative_to(vault)) for p in vault.rglob("*") if p.is_file())


def _note(vault, folder, prefix):
    matches = list((vault / folder).glob(f"{prefix}-*.md"))
    assert len(matches) == 1, matches
    return matches[0]


@pytest.fixture
def vault(tmp_path):
    return tmp_path / "vault"


def test_export_writes_notes_frontmatter_links_and_index(mem, vault):
    add(mem, PEOPLE, EDGES)
    res = export_vault(vault)
    assert not res.refused and len(res.created) == 5  # 4 notes + index
    assert _files(vault) == sorted([
        MANIFEST, "index.md",
        f"decisions/target-school-{mem.find_nodes('Target school')[0].id}.md",
        f"people/me-{mem.find_nodes('Me')[0].id}.md",
        f"projects/agent-aware-power-manager-{mem.find_nodes('Agent-Aware Power Manager')[0].id}.md",
        f"projects/agent-room-{mem.find_nodes('Agent Room')[0].id}.md",
    ])
    pm = _note(vault, "projects", "agent-aware-power-manager").read_text()
    front, body = pm.split("---\n")[1], pm.split("---\n", 2)[2]
    node = mem.find_nodes("Agent-Aware Power Manager")[0]
    for line in (f"id: {node.id}", "type: project", "status: active", f'created: "{node.created_at}"',
                 f'updated: "{node.updated_at}"', 'name: "Agent-Aware Power Manager"', "sources:", '  - "session:s"'):
        assert line in front.splitlines()
    room_id = mem.find_nodes("Agent Room")[0].id
    assert "Keeps the Mac awake while agents work." in body
    assert f"## Related\n\n- part_of [[projects/agent-room-{room_id}|Agent Room]]" in body

    room_note = _note(vault, "projects", "agent-room").read_text()
    assert f"- [[projects/agent-aware-power-manager-{node.id}|Agent-Aware Power Manager]] part_of this" in room_note
    assert "[[people/me-" in room_note

    index = (vault / "index.md").read_text()
    assert "## People" in index and "## Projects" in index and "## Decisions" in index
    assert f"[[projects/agent-room-{room_id}|Agent Room]]" in index
    assert "keep this folder private and local" in index


def test_export_is_deterministic_and_idempotent(mem, vault):
    add(mem, PEOPLE, EDGES)
    export_vault(vault)
    before = {p: (vault / p).read_text() for p in _files(vault)}
    res = export_vault(vault)
    assert res.created == [] and res.updated == [] and res.removed == []
    assert len(res.unchanged) == 5
    assert {p: (vault / p).read_text() for p in _files(vault)} == before


def test_reexport_updates_adds_and_removes_forgotten(mem, vault):
    add(mem, PEOPLE, EDGES)
    export_vault(vault)
    (vault / "my-own-note.md").write_text("mine")  # not written by room: must survive
    add(mem, [{"type": "project", "name": "Agent Room", "summary": "Now with a vault export.", "confidence": 0.9},
              {"type": "skill", "name": "SQLite", "summary": "Uses FTS5.", "confidence": 0.8}])
    pm = mem.find_nodes("Agent-Aware Power Manager")[0]
    mem.forget(pm.id)
    res = export_vault(vault)
    assert any(p.startswith("skills/sqlite-") for p in res.created)
    assert any(p.startswith("projects/agent-room-") for p in res.updated)
    assert "index.md" in res.updated
    assert res.removed == [f"projects/agent-aware-power-manager-{pm.id}.md"]
    assert not list((vault / "projects").glob("agent-aware-power-manager-*"))
    assert "Now with a vault export." in _note(vault, "projects", "agent-room").read_text()
    assert "Agent-Aware" not in (vault / "index.md").read_text()
    assert (vault / "my-own-note.md").read_text() == "mine"


def test_purged_memory_empties_the_vault(mem, vault, isolated_home):
    add(mem, PEOPLE, EDGES)
    export_vault(vault)
    mem.close()
    assert cli.main(["memory", "purge", "--yes"]) == 0
    res = export_vault(vault)
    assert len(res.removed) == 4
    assert _files(vault) == [MANIFEST, "index.md"]
    assert "_Memory is empty._" in (vault / "index.md").read_text()
    assert not (vault / "people").exists()


def test_superseded_excluded_by_default_and_marked_when_included(mem, vault):
    add(mem, [{"type": "decision", "name": "Target school", "summary": "Apply to Westbrook first.", "confidence": 0.7}],
        ts="2026-01-01T00:00:00Z")
    add(mem, [{"type": "decision", "name": "Target school", "summary": "Apply to Northfield first.", "confidence": 0.7}],
        ts="2026-06-01T00:00:00Z")
    export_vault(vault)
    notes = list((vault / "decisions").glob("*.md"))
    assert len(notes) == 1 and "Northfield" in notes[0].read_text() and "Westbrook" not in notes[0].read_text()

    res = export_vault(vault, include_superseded=True)
    notes = {p.read_text() for p in (vault / "decisions").glob("*.md")}
    old = next(n for n in notes if "Westbrook" in n)
    new = next(n for n in notes if "Northfield" in n)
    assert "status: superseded" in old and "SUPERSEDED: this is outdated history" in old
    assert "status: active" in new and "SUPERSEDED" not in new
    assert "- supersedes [[decisions/target-school-" in new and "(superseded)" in new
    assert "## Superseded (history, not current)" in (vault / "index.md").read_text()
    assert len(res.created) == 1

    res = export_vault(vault)  # dropping the flag removes the history note again
    assert len(res.removed) == 1 and len(list((vault / "decisions").glob("*.md"))) == 1


def test_vault_permissions_are_private(mem, vault):
    add(mem, PEOPLE, EDGES)
    export_vault(vault)
    assert stat.S_IMODE(vault.stat().st_mode) == 0o700
    assert stat.S_IMODE((vault / "projects").stat().st_mode) == 0o700
    for p in vault.rglob("*.md"):
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_refuses_vault_inside_a_git_work_tree(mem, tmp_path, capsys):
    repo = tmp_path / "some-repo"
    (repo / ".git").mkdir(parents=True)
    with pytest.raises(VaultSafetyError, match="git work tree"):
        export_vault(repo / "notes" / "vault")
    assert not (repo / "notes").exists()

    assert cli.main(["memory", "export", "--vault", str(repo / "vault")]) == 2
    assert "--i-know-this-is-a-repo" in capsys.readouterr().err
    assert not (repo / "vault").exists()

    assert cli.main(["memory", "export", "--vault", str(repo / "vault"), "--i-know-this-is-a-repo"]) == 0
    assert (repo / "vault" / "index.md").exists()


def test_refuses_this_repository(mem):
    root = project_root()
    if root is None:
        pytest.skip("not running from a git checkout")
    with pytest.raises(VaultSafetyError, match="git work tree"):
        export_vault(root / "vault-test")
    assert not (root / "vault-test").exists()


def test_secrets_are_redacted_in_notes(mem, vault):
    add(mem, [{"type": "fact", "name": "Ollama setup", "confidence": 0.5,
               "summary": f"Local only. Old key was {FAKE_GOOGLE_KEY} and token: hunter2hunter2."}])
    res = export_vault(vault)
    assert not res.refused
    text = "".join(p.read_text() for p in vault.rglob("*.md"))
    assert FAKE_GOOGLE_KEY not in text and "hunter2hunter2" not in text
    assert "[REDACTED]" in text and "Local only." in text


def test_note_still_matching_a_secret_is_refused(mem, vault, monkeypatch, capsys):
    """If redaction ever misses something, the final check stops the note from being written."""
    add(mem, [{"type": "fact", "name": "Leaky", "summary": f"key {FAKE_GOOGLE_KEY}", "confidence": 0.5},
              {"type": "fact", "name": "Clean", "summary": "nothing secret", "confidence": 0.5}])
    monkeypatch.setattr(vault_mod, "redact_secrets", lambda text: text)  # simulate a redaction gap
    assert cli.main(["memory", "export", "--vault", str(vault)]) == 1
    assert "still matches a secret pattern" in capsys.readouterr().err
    assert not list(vault.rglob("leaky-*.md"))
    assert list(vault.rglob("clean-*.md"))
    assert FAKE_GOOGLE_KEY not in "".join(p.read_text() for p in vault.rglob("*") if p.is_file())


def test_env_key_values_never_reach_the_vault(mem, vault, monkeypatch):
    secret = "plain" + "words" + "butsecret42"
    monkeypatch.setenv("MUSE_API_KEY", secret)
    add(mem, [{"type": "fact", "name": "Muse", "summary": f"configured with {secret}", "confidence": 0.5}])
    export_vault(vault)
    assert secret not in "".join(p.read_text() for p in vault.rglob("*") if p.is_file())


def test_unmanaged_file_with_same_name_is_not_overwritten(mem, vault):
    add(mem, PEOPLE[:1])
    me = mem.find_nodes("Me")[0]
    (vault / "people").mkdir(parents=True)
    (vault / "people" / f"me-{me.id}.md").write_text("hand-written")
    res = export_vault(vault)
    assert res.refused and "not written by room" in res.refused[0][1]
    assert (vault / "people" / f"me-{me.id}.md").read_text() == "hand-written"


def test_manifest_entries_outside_the_vault_are_ignored(mem, vault, tmp_path):
    outside = tmp_path / "keep.md"
    outside.write_text("keep")
    vault.mkdir()
    (vault / MANIFEST).write_text(json.dumps({"version": 1, "files": ["../keep.md"]}))
    export_vault(vault)
    assert outside.read_text() == "keep"


def test_slugs_are_short_ascii_and_stable():
    assert slugify("Agent-Aware Power Manager") == "agent-aware-power-manager"
    assert slugify("Café São Paulo!") == "cafe-sao-paulo"
    assert slugify("???") == "node"
    assert len(slugify("x" * 200)) <= 29
    assert slugify("Agent-Aware Power Manager for every Mac") == "agent-aware-power-manager-for"


def test_export_without_memory_cli(capsys, tmp_path):
    vault = tmp_path / "v"
    assert cli.main(["memory", "export", "--vault", str(vault)]) == 0
    out = capsys.readouterr().out
    assert "1 files: 1 new" in out and "keep it local" in out
    assert not (tmp_path / "agent-room" / "memory.db").exists()


def test_long_names_keep_working_links(mem, vault):
    long_name = "Distributed Systems Reading Group 2026 Spring Cohort B"
    add(mem, [{"type": "project", "name": long_name, "summary": "Weekly papers.", "confidence": 0.6},
              PEOPLE[0]], [{"src": "Me", "relation": "works_on", "dst": long_name}])
    export_vault(vault)
    node = mem.find_nodes(long_name)[0]
    path = next((vault / "projects").glob("*.md"))
    assert len(path.stem) <= 31 and path.stem.endswith(f"-{node.id}")
    me = _note(vault, "people", "me").read_text()
    assert f"- works_on [[projects/{path.stem}|{long_name}]]" in me and "[REDACTED]" not in me
