"""旧地址跳转（308）登记/撤销、站点读取与搬迁联动的端到端测试。

运行：pytest tests/test_redirects.py
"""

from __future__ import annotations

import os
import tempfile

import pytest

# 必须在导入 app 之前指定数据库文件
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
os.environ["DOCS_DB_PATH"] = _TMP.name

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    db.DB_PATH = _TMP.name
    db.init_db()
    with TestClient(app) as c:
        yield c


def publish(client, rev, changes):
    resp = client.post("/publish", json={"expected_revision": rev, "changes": changes})
    assert resp.status_code == 200, resp.text
    return resp.json()["revision"]


def seed(client):
    """初始发布：/index.html、/about.html、/docs/guide.html，互相有站内链接。"""
    return publish(client, 0, [
        {"op": "upsert", "path": "/index.html",
         "content": '<h1 id="top">Home</h1><a href="/about.html">about</a> '
                    '<a href="/docs/guide.html">guide</a>'},
        {"op": "upsert", "path": "/about.html",
         "content": '<a href="/#top">home</a> <a href="/docs/guide.html">guide</a>'},
        {"op": "upsert", "path": "/docs/guide.html",
         "content": '<a href="/about.html">about</a>'},
    ])


# ---------- 站点读取：真实页 / 旧地址 / 未知路径 ----------

def test_01_site_reader_real_path_returns_html(client):
    seed(client)
    resp = client.get("/index.html")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert '<h1 id="top">Home</h1>' in resp.text


def test_02_site_reader_unknown_path_404(client):
    resp = client.get("/does/not/exist.html")
    assert resp.status_code == 404
    resp = client.head("/does/not/exist.html")
    assert resp.status_code == 404


def test_03_api_routes_take_precedence_over_catch_all(client):
    assert client.get("/pages").status_code == 200
    assert client.get("/revision").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/docs").status_code == 200  # Swagger UI 不受影响


# ---------- 跳转登记 ----------

def test_10_register_redirect_ok_without_revision_bump(client):
    rev_before = client.get("/revision").json()["revision"]
    resp = client.post("/redirects", json={
        "old_path": "/old-about.html",
        "target_path": "/about.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "registered": True,
        "old_path": "/old-about.html",
        "target_path": "/about.html",
    }
    # 登记不推进修订号
    assert client.get("/revision").json() == {"revision": rev_before}

    listed = client.get("/redirects").json()["redirects"]
    assert {"old_path": "/old-about.html", "target_path": "/about.html"} in listed


def test_11_old_address_returns_308_to_real_path(client):
    resp = client.get("/old-about.html", follow_redirects=False)
    assert resp.status_code == 308
    assert resp.headers["location"] == "/about.html"
    assert not resp.text  # 308 无正文

    # 跟随跳转最终拿到真实页面 HTML
    followed = client.get("/old-about.html", follow_redirects=True)
    assert followed.status_code == 200
    assert str(followed.url).endswith("/about.html")


def test_12_register_old_path_equal_real_page_fails(client):
    resp = client.post("/redirects", json={
        "old_path": "/about.html", "target_path": "/index.html",
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "old address is a real page"
    # 未写入
    assert client.get("/about.html", follow_redirects=False).status_code == 200


def test_13_register_duplicate_old_path_fails(client):
    resp = client.post("/redirects", json={
        "old_path": "/old-about.html", "target_path": "/about.html",
    })
    assert resp.status_code == 422
    reasons = [e["reason"] for e in resp.json()["errors"]]
    assert "old address already registered as a redirect" in reasons


def test_14_register_missing_target_fails(client):
    resp = client.post("/redirects", json={
        "old_path": "/x.html", "target_path": "/ghost.html",
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "target page not found"
    assert client.get("/x.html").status_code == 404


def test_15_register_target_is_another_redirect_fails_no_chain(client):
    # 目标是已登记旧地址 -> 拒绝（目标必须是当前真实页面），不形成跳转链
    resp = client.post("/redirects", json={
        "old_path": "/chain.html", "target_path": "/old-about.html",
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "target page not found"
    assert client.get("/chain.html").status_code == 404


def test_16_register_bad_path_shape_422(client):
    for body in (
        {"old_path": "relative.html", "target_path": "/about.html"},
        {"old_path": "/o.html", "target_path": "about.html"},
        {"old_path": "/o.html#frag", "target_path": "/about.html"},
        {"old_path": "/o.html?x=1", "target_path": "/about.html"},
        {"old_path": "/", "target_path": "/about.html"},
    ):
        assert client.post("/redirects", json=body).status_code == 422, body


# ---------- 发布/合入不得占用旧地址、不得删除跳转目标 ----------

def test_20_publish_cannot_occupy_old_address(client):
    rev = client.get("/revision").json()["revision"]
    resp = client.post("/publish", json={
        "expected_revision": rev,
        "changes": [{"op": "upsert", "path": "/old-about.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["reason"] == "old address is registered as a redirect"
    assert e["source"] == "/old-about.html"
    assert client.get("/revision").json() == {"revision": rev}
    # 旧地址仍是跳转
    assert client.get("/old-about.html", follow_redirects=False).status_code == 308


def test_21_publish_cannot_delete_redirect_target(client):
    rev = client.get("/revision").json()["revision"]
    resp = client.post("/publish", json={
        "expected_revision": rev,
        "changes": [
            {"op": "upsert", "path": "/index.html",
             "content": '<h1 id="top">Home</h1><a href="/docs/guide.html">guide</a>'},
            {"op": "upsert", "path": "/docs/guide.html", "content": "<p>g</p>"},
            {"op": "delete", "path": "/about.html"},
        ],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["reason"] == "redirect target page not found"
    assert e["target"] == "/about.html"
    assert client.get("/revision").json() == {"revision": rev}
    assert "/about.html" in client.get("/pages").json()["pages"]


def test_22_merge_cannot_occupy_old_address_nor_drop_target(client):
    rev = client.get("/revision").json()["revision"]
    resp = client.post("/merge", json={
        "base_revision": rev,
        "changes": [{"op": "upsert", "path": "/old-about.html", "content": "<p>x</p>"}],
    })
    assert resp.status_code == 422
    assert client.get("/revision").json() == {"revision": rev}

    resp = client.post("/merge", json={
        "base_revision": rev,
        "changes": [{"op": "delete", "path": "/about.html"}],
    })
    assert resp.status_code == 422
    assert client.get("/revision").json() == {"revision": rev}


# ---------- 搬迁：同一事务登记旧路径跳转、改指既有跳转 ----------

def test_30_relocate_registers_old_path_redirect_and_repoints(client):
    rev = client.get("/revision").json()["revision"]  # 1
    resp = client.post("/relocate", json={
        "expected_revision": rev,
        "old_path": "/about.html",
        "new_path": "/info/about.html",
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == rev + 1

    # 腾出的旧路径 -> 新路径 308
    r = client.get("/about.html", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/info/about.html"
    # 此前登记的跳转改指新路径，不形成链
    r = client.get("/old-about.html", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/info/about.html"

    # 跳转表内容正确（两条都直指真实页）
    redirects = {d["old_path"]: d["target_path"] for d in client.get("/redirects").json()["redirects"]}
    assert redirects == {
        "/about.html": "/info/about.html",
        "/old-about.html": "/info/about.html",
    }


def test_31_internal_hrefs_validate_against_real_pages_not_redirects(client):
    # 站内 href 仍按真实页面校验：index 中指向旧页的链接已在搬迁时改指真实新路径
    pages = client.get("/pages").json()["pages"]
    assert '<a href="/info/about.html">about</a>' in pages["/index.html"]
    assert '<a href="/about.html">about</a>' not in pages["/docs/guide.html"]
    assert '<a href="/info/about.html">about</a>' in pages["/docs/guide.html"]


def test_32_relocate_into_occupied_redirect_path_fails(client):
    rev = client.get("/revision").json()["revision"]  # 2
    resp = client.post("/relocate", json={
        "expected_revision": rev,
        "old_path": "/docs/guide.html",
        "new_path": "/about.html",  # 已被跳转占用
    })
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["reason"] == "new path is registered as a redirect"
    # 页面、跳转、修订号均不变
    assert client.get("/revision").json() == {"revision": rev}
    pages = client.get("/pages").json()["pages"]
    assert "/docs/guide.html" in pages and "/about.html" not in pages
    r = client.get("/about.html", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/info/about.html"


def test_33_relocate_source_that_is_redirect_old_path_fails(client):
    rev = client.get("/revision").json()["revision"]
    resp = client.post("/relocate", json={
        "expected_revision": rev,
        "old_path": "/old-about.html",  # 是跳转地址，不是真实页面
        "new_path": "/x.html",
    })
    assert resp.status_code == 422
    reasons = [e["reason"] for e in resp.json()["errors"]]
    assert "source page not found" in reasons
    assert "old path is registered as a redirect" in reasons
    assert client.get("/revision").json() == {"revision": rev}


def test_34_chain_relocate_keeps_redirects_flat(client):
    # 再次搬迁真实页 /info/about.html -> /about2.html：所有相关跳转继续直指新页
    rev = client.get("/revision").json()["revision"]  # 2
    resp = client.post("/relocate", json={
        "expected_revision": rev,
        "old_path": "/info/about.html",
        "new_path": "/about2.html",
    })
    assert resp.status_code == 200
    redirects = {d["old_path"]: d["target_path"] for d in client.get("/redirects").json()["redirects"]}
    assert redirects == {
        "/about.html": "/about2.html",
        "/old-about.html": "/about2.html",
        "/info/about.html": "/about2.html",
    }
    # 每条跳转的目标都是真实页面（站点读取一跳即达，无链）
    for old in redirects:
        r = client.get(old, follow_redirects=False)
        assert r.status_code == 308 and r.headers["location"] == "/about2.html"
    assert client.get("/about2.html").status_code == 200


# ---------- 撤销 ----------

def test_40_revoke_missing_returns_404(client):
    resp = client.post("/redirects/revoke", json={"old_path": "/nope.html"})
    assert resp.status_code == 404
    assert resp.json() == {"detail": "redirect not found", "old_path": "/nope.html"}

    resp = client.delete("/redirects/nope.html")
    assert resp.status_code == 404


def test_41_revoke_then_old_address_can_be_reused(client):
    rev = client.get("/revision").json()["revision"]  # 3

    # DELETE 形式撤销 /about.html
    resp = client.delete("/redirects/about.html")
    assert resp.status_code == 200
    assert resp.json() == {"old_path": "/about.html", "revoked": True}
    # 撤销不推进修订号
    assert client.get("/revision").json() == {"revision": rev}
    assert client.get("/about.html").status_code == 404

    # 撤销后才能复用旧地址：搬迁进入该路径
    resp = client.post("/relocate", json={
        "expected_revision": rev,
        "old_path": "/docs/guide.html",
        "new_path": "/about.html",
    })
    assert resp.status_code == 200, resp.text
    assert client.get("/about.html").status_code == 200

    # POST /redirects/revoke 形式撤销另一条
    rev2 = client.get("/revision").json()["revision"]  # 4
    resp = client.post("/redirects/revoke", json={"old_path": "/old-about.html"})
    assert resp.status_code == 200
    assert client.get("/revision").json() == {"revision": rev2}

    # 撤销后旧地址可重新登记（指向新的真实页）
    resp = client.post("/redirects", json={
        "old_path": "/old-about.html", "target_path": "/about.html",
    })
    assert resp.status_code == 200
    r = client.get("/old-about.html", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/about.html"


def test_42_revoke_does_not_affect_pages_or_revision(client):
    rev = client.get("/revision").json()["revision"]
    pages_before = client.get("/pages").json()["pages"]
    resp = client.post("/redirects/revoke", json={"old_path": "/info/about.html"})
    assert resp.status_code == 200
    assert client.get("/revision").json() == {"revision": rev}
    assert client.get("/pages").json()["pages"] == pages_before


# ---------- 历史快照 / 重启持久化 ----------

def test_50_historical_pages_snapshot_unchanged(client):
    # revision 1：搬迁前，/about.html 是真实页面、跳转尚未随搬迁产生
    rev1 = client.get("/pages", params={"revision": 1}).json()["pages"]
    assert set(rev1) == {"/index.html", "/about.html", "/docs/guide.html"}
    assert '<a href="/about.html">about</a>' in rev1["/index.html"]
    # 历史快照不受当前跳转影响
    assert client.get("/pages", params={"revision": 99}).status_code == 404


def test_51_redirects_persist_across_restart(client):
    before = client.get("/redirects").json()
    revision = client.get("/revision").json()

    # 重新初始化（模拟进程重启再次执行 init_db）：表保留，跳转仍在
    db.init_db()
    assert client.get("/revision").json() == revision
    assert client.get("/redirects").json() == before
    r = client.get("/old-about.html", follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "/about.html"


# ---------- 原有接口行为保持 ----------

def test_60_publish_merge_relocate_contracts_unchanged(client):
    rev = client.get("/revision").json()["revision"]
    # 过期修订号仍 409
    resp = client.post("/publish", json={"expected_revision": 1, "changes": []})
    assert resp.status_code == 409 and resp.json()["current"] == rev

    resp = client.post("/relocate", json={
        "expected_revision": 1, "old_path": "/about2.html", "new_path": "/z.html",
    })
    assert resp.status_code == 409

    # 入口页仍不可搬迁
    resp = client.post("/relocate", json={
        "expected_revision": rev, "old_path": "/index.html", "new_path": "/home.html",
    })
    assert resp.status_code == 422

    # 站内 href 仍按真实页面校验：链接到已登记旧地址 -> target page not found
    resp = client.post("/publish", json={
        "expected_revision": rev,
        "changes": [{"op": "upsert", "path": "/linker.html",
                     "content": '<a href="/old-about.html">old address</a>'}],
    })
    assert resp.status_code == 422
    e = resp.json()["errors"][0]
    assert e["source"] == "/linker.html"
    assert e["target"] == "/old-about.html"
    assert e["reason"] == "target page not found"
    assert client.get("/revision").json() == {"revision": rev}

    # 空发布仍推进修订号且链接整包校验通过
    resp = client.post("/publish", json={"expected_revision": rev, "changes": []})
    assert resp.status_code == 200
    assert resp.json()["revision"] == rev + 1
