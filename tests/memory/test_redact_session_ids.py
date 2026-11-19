"""The room's own session ids are not secrets; real secrets are still caught (fake values only)."""

import pytest

from room.memory.redact import find_secrets, redact_secrets

STAMP = "20261118T171815Z-3fa9c1"
IDS = [STAMP, f"manual-{STAMP}", f"clipboard-{STAMP}", f"mcp-{STAMP}", f"claude-code-{STAMP}"]

# Built from pieces so the literal never appears whole in the source.
FAKES = {
    "github": "gh" "p_" + "A1b2C3d4E5f6G7h8J9k0L1m2N3o4P5q6",
    "openai": "s" "k-" + "proj9Xy8Wv7Ut6Sr5Qp4On3Ml2Kj1",
    "aws": "AK" "IA" + "ABCDEFGHIJKLMNOP",
    "jwt": "ey" "J" + "hbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "random40": "Zq8Lr2Vx9Tk4Wm7Np1Bc6Hd3Gf5Js0Ya8Ue2Io7Q",
}


@pytest.mark.parametrize("sid", IDS)
def test_session_ids_survive(sid):
    assert redact_secrets(sid) == sid
    assert find_secrets(sid) == []
    line = f"source: clipboard {sid} via hermes"
    assert redact_secrets(line) == line and find_secrets(line) == []
    assert find_secrets(f"clipboard:{sid}") == []  # the vault's "source:session_id" form


@pytest.mark.parametrize("name", sorted(FAKES))
def test_real_secrets_still_caught(name):
    fake = FAKES[name]
    text = f"key here {fake} end"
    assert fake not in redact_secrets(text)
    assert find_secrets(text)


@pytest.mark.parametrize("disguised", [
    "clipboard-" + FAKES["random40"],  # a secret glued onto one of our prefixes
    f"clipboard-{STAMP}" + "Zq8Lr2Vx9Tk4Wm7Np1",  # an id with random text attached
    "clipboard-20261118T171815Z-3fa9cZ" + "Zq8Lr2Vx9Tk4",  # id-shaped but not hex, plus extra
    "manual-99999999T999999Z-abcdef" + "Q7w8E9r0T1y2U3",  # wrong length after the id shape
    "notours-20261118T171815Z-3fa9c1x9Q2w8E7r6",  # unknown prefix plus random tail
])
def test_lookalikes_are_not_whitelisted(disguised):
    assert redact_secrets(disguised) == "[REDACTED]"
    assert find_secrets(disguised) == ["high-entropy-token"]


def test_labelled_secrets_and_keys_still_caught():
    for text in ["password=hunter2hunter2", "api_key: 'abcdef123456'",
                 "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"]:
        assert find_secrets(text), text
        assert "[REDACTED]" in redact_secrets(text)


def test_clipboard_node_shows_id_and_exports_to_vault(isolated_home, tmp_path, capsys):
    """End to end: a clipboard-sourced node shows its id (MCP == CLI) and its vault note is written."""
    from room import cli
    from room.mcp_server import handle
    from room.memory.store import MemoryStore, Provenance
    from room.memory.vault import export_vault

    sid = f"clipboard-{STAMP}"
    with MemoryStore() as store:
        store.apply_extraction({"nodes": [{"type": "fact", "name": "Clip fact", "summary": "From the clipboard.",
                                           "confidence": 0.7}], "edges": []}, Provenance(sid, "hermes", "clipboard"))
        [node] = store.all_nodes()
    resp = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": "memory_show", "arguments": {"id": node.id}}})
    text = resp["result"]["content"][0]["text"]
    capsys.readouterr()
    cli.main(["memory", "show", str(node.id)])
    assert sid in text and text.endswith(capsys.readouterr().out.rstrip("\n"))

    res = export_vault(tmp_path / "vault")
    assert res.refused == []
    note = next((tmp_path / "vault").rglob("clip-fact-*.md")).read_text()
    assert f"clipboard:{sid}" in note
