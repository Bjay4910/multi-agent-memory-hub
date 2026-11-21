"""/api/node/<id>?neighbor_status=1 adds each neighbor's status. Additive: the default response is unchanged, because
an existing test pins the exact neighbor keys."""


def _lumen(client):
    _, graph = client.get("/api/graph")
    return next(n["id"] for n in graph["nodes"] if n["name"] == "Lumen Notes")


def test_neighbor_status_is_opt_in_and_distinguishes_duplicate_names(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    node_id = _lumen(client)
    status, node = client.get(f"/api/node/{node_id}?neighbor_status=1")
    assert status == 200
    assert all(set(n) == {"id", "name", "type", "relation", "direction", "status"} for n in node["neighbors"])
    same_name = [n for n in node["neighbors"] if n["name"] == "Lumen Notes sync storage"]
    assert sorted(n["status"] for n in same_name) == ["active", "superseded"]


def test_default_node_response_is_unchanged(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    node_id = _lumen(client)
    _, plain = client.get(f"/api/node/{node_id}")
    _, zero = client.get(f"/api/node/{node_id}?neighbor_status=0")
    assert all("status" not in n for n in plain["neighbors"]) and zero == plain


def test_bad_neighbor_status_value_is_400(serve, demo_db, tmp_path):
    client = serve(demo_db, tmp_path / "presence")
    for value in ("yes", "2", "", "1&neighbor_status=1"):
        status, body = client.get(f"/api/node/{_lumen(client)}?neighbor_status={value}")
        assert status == 400 and set(body) == {"error"}, value
