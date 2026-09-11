# -*- coding: utf-8 -*-
"""outbox 投递器（spec §9 投递契约的 gateway 侧）。

轮询 ``robot_outbox`` 的 ``pending`` 行，按 ``kind`` 分发投递，回写状态：

* ``remind`` / ``check`` / ``leaderboard`` → 群 Markdown；
* ``ding`` → DING（现行 ``dws`` 命令契约，实测窗口再定 API）；
* 未登记的 ``kind`` → 记失败（``attempts`` 达上限后自然转 ``failed``，
  不会卡死队列）。

投递语义是**至少一次**：发送成功但 ``mark_delivered`` 失败时，下一轮会
重发——与现行 stateFile 语义一致（现行也是发完才写 state，崩溃即重发），
不引入新的重复风险。

单行失败不中断批次；``last_error`` 只写**错误码**（非泄露约定），异常
原文永远不进 DB、不进日志行。
"""

from dataclasses import dataclass, field


class DeliveryError(RuntimeError):
    """投递无法执行（消息不得包含载荷、URL 或响应体）。"""


#: 群消息类 kind：走 DingTalkClient 的群 Markdown。
_GROUP_KINDS = frozenset({"remind", "check", "leaderboard"})

#: 每种 kind 的非泄露错误码（写 outbox.last_error）。
_ERROR_CODES = {
    "remind": "group_send_failed",
    "check": "group_send_failed",
    "leaderboard": "group_send_failed",
    "ding": "ding_send_failed",
}


@dataclass(frozen=True)
class DeliverReport:
    """一轮投递的安全摘要（只含 dedupe_key 与计数）。"""

    delivered: tuple = field(default_factory=tuple)
    failed: tuple = field(default_factory=tuple)

    @property
    def delivered_count(self):
        return len(self.delivered)

    @property
    def failed_count(self):
        return len(self.failed)


class OutboxDeliveryWorker:
    """轮询 outbox 并逐行投递。所有依赖注入，测试无需钉钉或 MySQL。"""

    def __init__(self, *, outbox, deliverer, now, sleep=None):
        self._outbox = outbox
        self._deliverer = deliverer
        self._now = now
        self._sleep = sleep

    def deliver_once(self):
        """取一批 ``pending`` 并逐行投递，返回 :class:`DeliverReport`。"""
        delivered = []
        failed = []
        for row in self._outbox.fetch_pending():
            if self._deliver(row):
                delivered.append(row["dedupe_key"])
            else:
                failed.append(row["dedupe_key"])
        return DeliverReport(delivered=tuple(delivered), failed=tuple(failed))

    def run_forever(self, *, interval_seconds=30):
        """长驻循环（容器入口）。``KeyboardInterrupt`` 正常退出。"""
        if self._sleep is None:
            raise DeliveryError("run_forever requires a sleep callable")
        while True:
            self.deliver_once()
            self._sleep(interval_seconds)

    # ------------------------------------------------------------------

    def _deliver(self, row):
        """投递单行。返回 True=delivered，False=已登记失败。"""
        kind = row["kind"]
        try:
            if kind in _GROUP_KINDS:
                self._deliverer.send_group(
                    region=row["region"],
                    title=row["title"],
                    body_md=row["body_md"],
                    at_user_ids=row["at_user_ids"],
                )
            elif kind == "ding":
                self._deliverer.send_ding(
                    region=row["region"],
                    user_ids=row["at_user_ids"],
                    content=row["body_md"],
                )
            else:
                raise DeliveryError("unknown outbox kind")
        except Exception:
            self._outbox.register_failure(
                row["dedupe_key"],
                error_code=_ERROR_CODES.get(kind, "unknown_outbox_kind"),
            )
            return False

        self._outbox.mark_delivered(
            row["dedupe_key"], delivered_at=self._now()
        )
        return True
