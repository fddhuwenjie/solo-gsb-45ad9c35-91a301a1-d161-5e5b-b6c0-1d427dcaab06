"""发布/合入/搬迁/跳转登记共用的事务异常类型。

单独成模块是为了打断 ``publish`` 与 ``redirects`` 之间的导入环：
两者都需要 :class:`PublishValidationError` 来沿用统一的 422 响应体。
"""

from __future__ import annotations


class PublishConflict(Exception):
    """客户端携带的 expected_revision 已过期。"""

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(f"expected revision is stale; current revision is {current_revision}")


class PublishValidationError(Exception):
    """整批改动试用后校验不通过；内容与修订号保持不变。"""

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__("publish validation failed")
