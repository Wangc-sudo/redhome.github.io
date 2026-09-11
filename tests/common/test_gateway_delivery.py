"""Tests for the outbox delivery worker and the DingTalk deliverer."""

import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

from common.gateway.delivery import DeliveryError, OutboxDeliveryWorker
from common.gateway.dingtalk_deliverer import (
    DingTalkDeliverer,
    DwsCommandDingSender,
)


_NOW = datetime(2026, 9, 11, 20, 0, tzinfo=timezone.utc)


def _row(kind, key=None, region="hangzhou", at=("u1",)):
    return {
        "dedupe_key": key or f"{region}:{kind}:2026-09-11",
        "region": region,
        "kind": kind,
        "title": "标题",
        "body_md": "正文",
        "at_user_ids": list(at),
    }


def _worker(rows):
    outbox = Mock()
    outbox.fetch_pending.return_value = list(rows)
    deliverer = Mock()
    worker = OutboxDeliveryWorker(
        outbox=outbox, deliverer=deliverer, now=lambda: _NOW,
    )
    return worker, outbox, deliverer


class WorkerRoutingTests(unittest.TestCase):

    def test_group_kinds_route_to_send_group(self):
        rows = [_row("remind"), _row("check"), _row("leaderboard")]
        worker, outbox, deliverer = _worker(rows)

        report = worker.deliver_once()

        self.assertEqual(deliverer.send_group.call_count, 3)
        deliverer.send_group.assert_any_call(
            region="hangzhou", title="标题", body_md="正文", at_user_ids=["u1"],
        )
        deliverer.send_ding.assert_not_called()
        self.assertEqual(outbox.mark_delivered.call_count, 3)
        outbox.mark_delivered.assert_any_call(
            "hangzhou:remind:2026-09-11", delivered_at=_NOW,
        )
        self.assertEqual(report.delivered_count, 3)
        self.assertEqual(report.failed_count, 0)

    def test_ding_routes_to_send_ding(self):
        worker, _, deliverer = _worker([_row("ding", at=("u1", "u2"))])

        report = worker.deliver_once()

        deliverer.send_ding.assert_called_once_with(
            region="hangzhou", user_ids=["u1", "u2"], content="正文",
        )
        deliverer.send_group.assert_not_called()
        self.assertEqual(report.delivered_count, 1)

    def test_failure_registers_code_and_does_not_abort_the_batch(self):
        rows = [_row("remind", key="k1"), _row("check", key="k2")]
        worker, outbox, deliverer = _worker(rows)
        deliverer.send_group.side_effect = [RuntimeError("secret-host"), None]

        report = worker.deliver_once()

        outbox.register_failure.assert_called_once_with(
            "k1", error_code="group_send_failed",
        )
        outbox.mark_delivered.assert_called_once_with(
            "k2", delivered_at=_NOW,
        )
        self.assertEqual(report.failed, ("k1",))
        self.assertEqual(report.delivered, ("k2",))

    def test_ding_failure_uses_the_ding_error_code(self):
        worker, outbox, deliverer = _worker([_row("ding", key="k1")])
        deliverer.send_ding.side_effect = RuntimeError("boom")

        worker.deliver_once()

        outbox.register_failure.assert_called_once_with(
            "k1", error_code="ding_send_failed",
        )

    def test_unknown_kind_never_reaches_the_deliverer(self):
        worker, outbox, deliverer = _worker([_row("sms", key="k1")])

        report = worker.deliver_once()

        deliverer.send_group.assert_not_called()
        deliverer.send_ding.assert_not_called()
        outbox.register_failure.assert_called_once_with(
            "k1", error_code="unknown_outbox_kind",
        )
        self.assertEqual(report.failed, ("k1",))

    def test_empty_outbox_is_a_noop(self):
        worker, outbox, deliverer = _worker([])

        report = worker.deliver_once()

        deliverer.send_group.assert_not_called()
        outbox.mark_delivered.assert_not_called()
        self.assertEqual(report.delivered_count, 0)
        self.assertEqual(report.failed_count, 0)

    def test_run_forever_requires_a_sleep_callable(self):
        worker, _, _ = _worker([])
        with self.assertRaises(DeliveryError):
            worker.run_forever()


class DingTalkDelivererTests(unittest.TestCase):

    _CONFIG = {
        "hangzhou": {
            "robot_code": "rc-hz",
            "open_conversation_id": "conv-hz",
            "group_name": "杭州日报群",
        },
    }

    def _deliverer(self, ding_sender=None):
        client = Mock()
        deliverer = DingTalkDeliverer(
            client=client,
            region_config=self._CONFIG,
            ding_sender=ding_sender or Mock(),
        )
        return deliverer, client

    def test_send_group_uses_region_config_and_passes_at_ids(self):
        deliverer, client = self._deliverer()
        deliverer.send_group(
            region="hangzhou", title="t", body_md="b", at_user_ids=["u1"],
        )
        client.send_group_markdown.assert_called_once_with(
            "rc-hz", "conv-hz", "t", "b", at_user_ids=["u1"],
        )

    def test_send_group_empty_at_ids_becomes_none(self):
        deliverer, client = self._deliverer()
        deliverer.send_group(region="hangzhou", title="t", body_md="b",
                             at_user_ids=[])
        self.assertIsNone(
            client.send_group_markdown.call_args.kwargs["at_user_ids"]
        )

    def test_send_group_unknown_region_raises(self):
        deliverer, _ = self._deliverer()
        with self.assertRaises(DeliveryError):
            deliverer.send_group(
                region="nowhere", title="t", body_md="b", at_user_ids=[],
            )

    def test_send_ding_routes_robot_code_from_region_config(self):
        sender = Mock()
        deliverer, _ = self._deliverer(ding_sender=sender)
        deliverer.send_ding(region="hangzhou", user_ids=["u1"], content="c")
        sender.assert_called_once_with(
            robot_code="rc-hz", user_ids=["u1"], content="c",
        )

    def test_send_ding_without_users_is_a_noop(self):
        sender = Mock()
        deliverer, _ = self._deliverer(ding_sender=sender)
        deliverer.send_ding(region="hangzhou", user_ids=[], content="c")
        sender.assert_not_called()

    def test_send_ding_unknown_region_raises(self):
        deliverer, _ = self._deliverer()
        with self.assertRaises(DeliveryError):
            deliverer.send_ding(region="nowhere", user_ids=["u1"], content="c")


class DwsCommandDingSenderTests(unittest.TestCase):

    def test_emits_the_legacy_contract_verbatim(self):
        lines = []
        sender = DwsCommandDingSender(emit=lines.append)
        sender(robot_code="rc-1", user_ids=["u1", "u2"], content="内容X")

        self.assertEqual(lines, [
            "DING_CMD_START",
            'dws ding message send --robot-code rc-1 --users u1,u2 '
            '--content "内容X" --type app --format json',
            "DING_CMD_END",
        ])


if __name__ == "__main__":
    unittest.main()
