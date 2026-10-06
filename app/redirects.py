"""旧地址跳转（308）登记、撤销与一致性维护。

跳转是文档包的**当前状态**（不是页面修订）：

- ``old_path`` 沿用现有页面路径格式（以 ``/`` 开头、无查询串/片段），
  且既不能是真实页面，也不能被另一条跳转占用；
- ``target_path`` 必须是当前真实页面——绝不允许指向另一条旧地址，
  因此不会出现跳转链；
- 登记与撤销都在单个 ``BEGIN IMMEDIATE`` 写事务内完成，**不推进页面
  修订号**，随库持久化，进程重启后仍然有效；
- 发布/合入/搬迁在同一写事务内通过 :func:`check_redirect_consistency`
  守住两条不变量：最终页面集合不得占用任何旧地址，仍被跳转指向的
  页面不得删除；违反则整批回滚，页面、跳转与修订号均不变；
- 搬迁（:mod:`app.relocate`）在同一写事务内把腾出的旧路径登记为
  指向新路径的跳转，并把此前指向旧页的跳转改指新路径；撤销跳转后
  旧地址才可被复用。
"""

from __future__ import annotations

import sqlite3

from .errors import PublishValidationError


class RedirectNotFound(Exception):
    """撤销一个未登记的旧地址。"""

    def __init__(self, old_path: str) -> None:
        self.old_path = old_path
        super().__init__(f"redirect not found: {old_path}")


def load_redirects(conn: sqlite3.Connection) -> dict[str, str]:
    """读取当前全部跳转（旧地址 -> 真实页面路径），按旧地址排序。"""
    rows = conn.execute(
        "SELECT old_path, target_path FROM redirects ORDER BY old_path"
    ).fetchall()
    return {row["old_path"]: row["target_path"] for row in rows}


def redirect_target(conn: sqlite3.Connection, old_path: str) -> str | None:
    """旧地址当前登记的跳转目标；未登记返回 None。"""
    row = conn.execute(
        "SELECT target_path FROM redirects WHERE old_path = ?", (old_path,)
    ).fetchone()
    return None if row is None else row["target_path"]


def check_redirect_consistency(
    pages: dict[str, str], redirects: dict[str, str]
) -> list[dict[str, str]]:
    """核对最终页面集合与跳转登记之间的不变量（不写库）。

    - 页面不得占用已登记的旧地址（旧地址只能先撤销再复用）；
    - 每条跳转的目标必须仍是真实页面（仍被跳转指向的页面不得删除）。
    """
    errors: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(source: str, target: str, reason: str) -> None:
        key = f"{source}|{target}|{reason}"
        if key not in seen:
            seen.add(key)
            errors.append({"source": source, "target": target, "reason": reason})

    for old_path in sorted(redirects):
        if old_path in pages:
            add(old_path, old_path, "old address is registered as a redirect")
        target = redirects[old_path]
        if target not in pages:
            add("", target, "redirect target page not found")
    return errors


def register_redirect(
    conn: sqlite3.Connection, old_path: str, target_path: str
) -> dict[str, str]:
    """在单个写事务内登记一条旧地址 -> 真实页面的 308 跳转。

    不推进页面修订号。旧地址为真实页面或已被其他跳转占用、目标不是
    当前真实页面时抛 :class:`PublishValidationError`，事务回滚，
    跳转与修订号均不变。成功返回 ``{"old_path", "target_path"}``。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()
        current = row["current_revision"]

        page_exists = conn.execute(
            "SELECT 1 FROM pages WHERE revision = ? AND path = ? LIMIT 1",
            (current, target_path),
        ).fetchone()

        errors: list[dict[str, str]] = []
        if conn.execute(
            "SELECT 1 FROM redirects WHERE old_path = ? LIMIT 1", (old_path,)
        ).fetchone():
            errors.append(
                {
                    "source": old_path,
                    "target": old_path,
                    "reason": "old address already registered as a redirect",
                }
            )
        if conn.execute(
            "SELECT 1 FROM pages WHERE revision = ? AND path = ? LIMIT 1",
            (current, old_path),
        ).fetchone():
            errors.append(
                {"source": old_path, "target": old_path, "reason": "old address is a real page"}
            )
        if page_exists is None:
            errors.append(
                {"source": old_path, "target": target_path, "reason": "target page not found"}
            )
        if errors:
            raise PublishValidationError(errors)

        conn.execute(
            "INSERT INTO redirects (old_path, target_path) VALUES (?, ?)",
            (old_path, target_path),
        )
        conn.commit()
        return {"old_path": old_path, "target_path": target_path}
    except Exception:
        conn.rollback()
        raise


def revoke_redirect(conn: sqlite3.Connection, old_path: str) -> dict[str, str]:
    """在单个写事务内撤销一条跳转，使旧地址随后可以复用。

    不推进页面修订号。旧地址未登记抛 :class:`RedirectNotFound`（404），
    事务回滚。成功返回 ``{"old_path", "revoked": true}``。
    """
    try:
        conn.execute("BEGIN IMMEDIATE")

        target = redirect_target(conn, old_path)
        if target is None:
            raise RedirectNotFound(old_path)

        conn.execute("DELETE FROM redirects WHERE old_path = ?", (old_path,))
        conn.commit()
        return {"old_path": old_path, "revoked": True}
    except Exception:
        conn.rollback()
        raise
