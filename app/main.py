"""FastAPI 入口：静态文档包链接一致性发布 API。"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator

from . import db
from .errors import PublishConflict, PublishValidationError
from .merge import MergeBaselineNotFound, MergeConflict, merge
from .publish import publish
from .redirects import (
    RedirectNotFound,
    load_redirects,
    register_redirect,
    redirect_target,
    revoke_redirect,
)
from .relocate import relocate


def _validate_site_path(v: str) -> str:
    """页面/跳转共用的路径格式：以 / 开头、非空、无查询串与片段。"""
    if not v.startswith("/"):
        raise ValueError("path must start with '/'")
    if len(v) < 2:
        raise ValueError("path must not be empty")
    if "#" in v or "?" in v:
        raise ValueError("path must not contain query string or fragment")
    return v


class RelocateRequest(BaseModel):
    """页面搬迁：把已发布的旧页面移动到未占用的新路径，并重写受影响的站内 href。"""

    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")
    old_path: str = Field(description="已发布的旧页面路径，必须以 / 开头，且不能是 /index.html")
    new_path: str = Field(description="未占用的新页面路径，必须以 / 开头，且不能与旧路径相同")

    @field_validator("old_path", "new_path")
    @classmethod
    def _validate_relocate_path(cls, v: str) -> str:
        return _validate_site_path(v)


class Change(BaseModel):
    """单条变更：新增/替换页面（upsert）或删除页面（delete）。"""

    op: Literal["upsert", "delete", "put"] = Field(
        description="upsert/put=新增或替换；delete=删除"
    )
    path: str = Field(description="页面路径，必须以 / 开头，如 /docs/intro.html")
    content: str | None = Field(default=None, description="upsert 时的完整 HTML 内容")

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        return _validate_site_path(v)

    @field_validator("content")
    @classmethod
    def _validate_content(cls, v, info):
        op = info.data.get("op")
        if op in ("upsert", "put") and v is None:
            raise ValueError("content is required for upsert")
        return v


class PublishRequest(BaseModel):
    expected_revision: int = Field(ge=0, description="客户端预期的当前修订号，实现乐观锁")
    changes: list[Change] = Field(default_factory=list)


class MergeRequest(BaseModel):
    """并行合入：基于历史基线修订，把沿用发布接口格式的页面变更合入当前快照。"""

    base_revision: int = Field(ge=0, description="本端改动所基于的历史修订号（0 为空基线）")
    changes: list[Change] = Field(default_factory=list)


class RedirectRequest(BaseModel):
    """旧地址跳转登记：旧地址沿用页面路径格式，目标必须是当前真实页面。"""

    old_path: str = Field(description="旧地址，必须以 / 开头，且不是真实页面/其他跳转")
    target_path: str = Field(description="跳转目标，必须是当前真实页面路径，以 / 开头")

    @field_validator("old_path", "target_path")
    @classmethod
    def _validate_redirect_path(cls, v: str) -> str:
        return _validate_site_path(v)


class RedirectRevokeRequest(BaseModel):
    """撤销旧地址跳转：撤销后该旧地址才能被重新登记或作为页面路径复用。"""

    old_path: str = Field(description="要撤销跳转的旧地址，必须以 / 开头")

    @field_validator("old_path")
    @classmethod
    def _validate_redirect_path(cls, v: str) -> str:
        return _validate_site_path(v)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield


app = FastAPI(
    title="静态文档包链接一致性发布 API",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/revision")
def current_revision():
    conn = db.connect()
    try:
        row = conn.execute("SELECT current_revision FROM meta WHERE id = 1").fetchone()
        return {"revision": row["current_revision"]}
    finally:
        conn.close()


@app.get("/pages")
def list_pages(revision: int | None = None):
    """返回同一修订的完整页面集合（path -> content）。

    不传 revision 时返回当前修订。revision 不存在返回 404。
    """
    conn = db.connect()
    try:
        current = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]
        rev = current if revision is None else revision
        if rev < 0 or rev > current:
            raise HTTPException(status_code=404, detail=f"revision {rev} not found")
        if rev == 0:
            return {"revision": 0, "pages": {}}
        rows = conn.execute(
            "SELECT path, content FROM pages WHERE revision = ? ORDER BY path", (rev,)
        ).fetchall()
        return {"revision": rev, "pages": {row["path"]: row["content"] for row in rows}}
    finally:
        conn.close()


@app.post("/publish")
def publish_endpoint(req: PublishRequest):
    """批量新增/替换/删除页面。校验与发布在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = publish(
            conn,
            expected_revision=req.expected_revision,
            changes=[c.model_dump() for c in req.changes],
        )
        return {"revision": new_revision, "published": True}
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.post("/merge")
def merge_endpoint(req: MergeRequest):
    """基于历史基线修订的并行变更合入。比对、合入、校验与提交在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = merge(
            conn,
            base_revision=req.base_revision,
            changes=[c.model_dump() for c in req.changes],
        )
        return {"revision": new_revision, "merged": True, "base_revision": req.base_revision}
    except MergeConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "merge conflict",
                "base_revision": req.base_revision,
                "current": exc.current_revision,
                "conflicts": exc.conflicts,
            },
        )
    except MergeBaselineNotFound as exc:
        return JSONResponse(
            status_code=404,
            content={
                "detail": "base revision not found",
                "base_revision": exc.base_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.post("/relocate")
def relocate_endpoint(req: RelocateRequest):
    """搬迁单个已发布页面并改写受影响的站内 href。校验与发布在同一 SQLite 写事务内完成。"""
    conn = db.connect()
    try:
        new_revision = relocate(
            conn,
            expected_revision=req.expected_revision,
            old_path=req.old_path,
            new_path=req.new_path,
        )
        return {
            "revision": new_revision,
            "relocated": True,
            "old_path": req.old_path,
            "new_path": req.new_path,
        }
    except PublishConflict as exc:
        return JSONResponse(
            status_code=409,
            content={
                "detail": "revision conflict",
                "expected": req.expected_revision,
                "current": exc.current_revision,
            },
        )
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


@app.get("/redirects")
def list_redirects():
    """列出当前全部旧地址跳转（登记/撤销不推进修订号，此处始终是当前状态）。"""
    conn = db.connect()
    try:
        return {"redirects": [{"old_path": k, "target_path": v} for k, v in load_redirects(conn).items()]}
    finally:
        conn.close()


@app.post("/redirects")
def register_redirect_endpoint(req: RedirectRequest):
    """登记旧地址 -> 当前真实页面的 308 跳转。不推进页面修订号。"""
    conn = db.connect()
    try:
        result = register_redirect(conn, old_path=req.old_path, target_path=req.target_path)
        return {"registered": True, **result}
    except PublishValidationError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": "validation failed", "errors": exc.errors},
        )
    finally:
        conn.close()


def _revoke(old_path: str):
    """撤销跳转的共用实现：未登记返回 404，成功不推进修订号。"""
    conn = db.connect()
    try:
        return revoke_redirect(conn, old_path=old_path)
    except RedirectNotFound as exc:
        return JSONResponse(
            status_code=404,
            content={"detail": "redirect not found", "old_path": exc.old_path},
        )
    finally:
        conn.close()


@app.post("/redirects/revoke")
def revoke_redirect_endpoint(req: RedirectRevokeRequest):
    """撤销旧地址跳转（POST JSON 形式），撤销后旧地址才可复用。不推进修订号。"""
    return _revoke(req.old_path)


@app.delete("/redirects/{old_path:path}")
def delete_redirect_endpoint(old_path: str):
    """撤销旧地址跳转（REST 形式 DELETE /redirects/<旧地址>）。"""
    return _revoke("/" + old_path)


# 站点读取：真实路径返回 HTML；已登记旧地址 308 跳到真实路径；其余 404。
# 必须在所有 API 路由之后声明（catch-all），仅匹配 GET/HEAD，不影响 /docs 等。
@app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def site_reader(full_path: str):
    path = "/" + full_path

    conn = db.connect()
    try:
        current = conn.execute(
            "SELECT current_revision FROM meta WHERE id = 1"
        ).fetchone()["current_revision"]

        row = conn.execute(
            "SELECT content FROM pages WHERE revision = ? AND path = ?",
            (current, path),
        ).fetchone()
        if row is not None:
            return HTMLResponse(row["content"])

        target = redirect_target(conn, path)
        if target is not None:
            # 308 永久跳转：方法与请求体不变，旧地址在搬迁后仍可访问。
            return RedirectResponse(url=target, status_code=308)

        # 未知路径：不是真实页面，也没有跳转登记。
        return JSONResponse(
            status_code=404,
            content={"detail": "page not found", "path": path},
        )
    finally:
        conn.close()
