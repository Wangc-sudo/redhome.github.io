# -*- coding: utf-8 -*-
"""Tests for the ecom-people extract (raw 钉钉表 -> fact_daily_report_offline).

纯 unittest + 手写 fake：不连真 DB、不调真钉钉。mock 源数据用 raw 行 dict
列表，``responsible_person`` 用真实落库形态（JSON 数组字符串
``[{"name": ..., "unionId": ...}]``）。DB fake 按 SQL 动词路由并维护一张
内存表，因此可以真实验证「同业务键重放只更新不插重复行」。
"""

import json
import unittest
from datetime import datetime, timezone

from common.public_data.extract_ecom_people import (
    ECOM_RUN_ID,
    REGION,
    build_fact_rows,
    fetch_source_rows,
    parse_owners,
    store_meta_by_store,
    upsert_rows,
)


_NOW = datetime(2026, 9, 18, 3, 0, tzinfo=timezone.utc)


def _owners_json(owners):
    """raw ``responsible_person`` 真实落库形态：JSON 数组字符串。"""
    return json.dumps(
        [{"name": name, "unionId": f"union-{name}"} for name in owners],
        ensure_ascii=False,
    )


def _detail_row(store, owners, sales, day, channel="天猫"):
    """``channel_daily_sales`` 的 raw 行 dict。"""
    return {
        "channel": channel,
        "store_name": store,
        "business_date": day,
        "sales_amount": sales,
        "responsible_person": _owners_json(owners),
    }


def _target_row(store, target, channel="天猫", owners=()):
    """``channel_monthly_target`` 的 raw 行 dict。"""
    return {
        "store_name": store,
        "channel": channel,
        "monthly_target": target,
        "responsible_person": _owners_json(owners),
    }


def _store_meta(mapping):
    """``{店铺: (target, owners)}`` → :func:`store_meta_by_store` 的产物形态。"""
    return {
        store: {"target": target, "owners": list(owners)}
        for store, (target, owners) in mapping.items()
    }


# ---------------------------------------------------------------------------
# DB fake：按 SQL 动词路由，维护内存表 {source_record_id: row}
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, conn):
        self._conn = conn
        self._one = None

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        head = sql.lstrip().upper()
        if head.startswith("SELECT"):
            region, owner, day = params
            self._one = None
            for row in self._conn.table.values():
                if (row["region"] == region
                        and row["responsible_person"] == owner
                        and row["business_date"] == day):
                    self._one = dict(row)
                    return
        elif head.startswith("UPDATE"):
            sales, target, synced, source_record_id = params
            row = self._conn.table[source_record_id]
            row["sales_amount"] = sales
            row["monthly_target"] = target
            row["synced_at"] = synced
        elif head.startswith("INSERT"):
            (source_record_id, region, owner, department, day, sales,
             daily_target, monthly_target, note, synced, run_id) = params
            self._conn.table[source_record_id] = {
                "source_record_id": source_record_id,
                "region": region,
                "responsible_person": owner,
                "department": department,
                "business_date": day,
                "sales_amount": sales,
                "daily_target": daily_target,
                "monthly_target": monthly_target,
                "note": note,
                "synced_at": synced,
                "sync_run_id": run_id,
            }

    def fetchone(self):
        return self._one

    def close(self):
        pass


class _FakeConnection:
    def __init__(self, table=None):
        self.table = {} if table is None else table
        self.executed = []

    def cursor(self):
        return _Cursor(self)

    def insert_count(self):
        return sum(1 for sql, _ in self.executed
                   if sql.lstrip().upper().startswith("INSERT"))

    def update_count(self):
        return sum(1 for sql, _ in self.executed
                   if sql.lstrip().upper().startswith("UPDATE"))


# ---------------------------------------------------------------------------
# raw 读取 fake：按 SQL 中的表名返回预置行
# ---------------------------------------------------------------------------

class _RawCursor:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        if "channel_monthly_target" in sql:
            self._rows = list(self._conn.target_rows)
        else:
            self._rows = list(self._conn.detail_rows)

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def close(self):
        pass


class _RawConnection:
    def __init__(self, detail_rows, target_rows):
        self.detail_rows = detail_rows
        self.target_rows = target_rows
        self.executed = []

    def cursor(self):
        return _RawCursor(self)


# ---------------------------------------------------------------------------
# 负责人集合解析（raw JSON 形态）
# ---------------------------------------------------------------------------

class ParseOwnersTests(unittest.TestCase):

    def test_single_owner(self):
        self.assertEqual(parse_owners(_owners_json(["张三"])), ["张三"])

    def test_multi_owner_set(self):
        value = _owners_json(["张三", "李四"])
        self.assertEqual(parse_owners(value), ["张三", "李四"])

    def test_strips_and_dedupes(self):
        value = json.dumps(
            [{"name": "张三"}, {"name": " 李四 "}, {"name": "张三"}],
            ensure_ascii=False,
        )
        self.assertEqual(parse_owners(value), ["张三", "李四"])

    def test_parsed_list_and_dict_forms(self):
        self.assertEqual(parse_owners([{"name": "张三"}, "李四"]), ["张三", "李四"])
        self.assertEqual(parse_owners({"name": "张三"}), ["张三"])

    def test_comma_string_fallback(self):
        """历史脏数据若是逗号串，按逗号拆分兜底。"""
        self.assertEqual(parse_owners("张三, 李四 ,张三"), ["张三", "李四"])

    def test_empty_set(self):
        self.assertEqual(parse_owners(None), [])
        self.assertEqual(parse_owners("null"), [])
        self.assertEqual(parse_owners("[]"), [])
        self.assertEqual(parse_owners("  "), [])


# ---------------------------------------------------------------------------
# 行展开（纯逻辑）
# ---------------------------------------------------------------------------

class BuildFactRowsTests(unittest.TestCase):

    def test_single_store_single_owner_one_row(self):
        rows = build_fact_rows(
            [_detail_row("旗舰店", ["张三"], 12800, "2026-09-10")],
            _store_meta({"旗舰店": (300000.0, ["张三"])}),
        )
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_record_id"], "ecom:张三:2026-09-10")
        self.assertEqual(row["region"], REGION)
        self.assertEqual(row["responsible_person"], "张三")
        self.assertEqual(row["department"], "天猫")
        self.assertEqual(row["business_date"], "2026-09-10")
        self.assertEqual(row["sales_amount"], 12800.0)
        self.assertEqual(row["monthly_target"], 300000.0)

    def test_two_owners_each_get_full_store_sales(self):
        """多人共一店各计整店：金额不切分、不只取第一个。"""
        rows = build_fact_rows(
            [_detail_row("旗舰店", ["张三", "李四"], 12800, "2026-09-10")],
            _store_meta({"旗舰店": (300000.0, ["张三", "李四"])}),
        )
        self.assertEqual(len(rows), 2)
        owners = {row["responsible_person"] for row in rows}
        self.assertEqual(owners, {"张三", "李四"})
        for row in rows:
            self.assertEqual(row["sales_amount"], 12800.0)
            self.assertEqual(row["monthly_target"], 300000.0)

    def test_monthly_target_melts_across_days(self):
        """月目标 melt：同一 owner 多日行 monthly_target 相同。"""
        detail_rows = [
            _detail_row("旗舰店", ["张三"], 100, "2026-09-10"),
            _detail_row("旗舰店", ["张三"], 200, "2026-09-11"),
        ]
        rows = build_fact_rows(detail_rows, _store_meta({"旗舰店": (300000.0, ["张三"])}))
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["monthly_target"] for row in rows}, {300000.0})

    def test_store_without_target_gets_none(self):
        rows = build_fact_rows(
            [_detail_row("小店", ["王五"], 50, "2026-09-10", channel="京东")],
            _store_meta({}),
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["monthly_target"])

    def test_empty_owners_row_is_skipped(self):
        detail_rows = [
            _detail_row("旗舰店", [], 100, "2026-09-10"),
            {"channel": "天猫", "store_name": "老店", "business_date": "2026-09-10",
             "sales_amount": 100, "responsible_person": None},
            _detail_row("旗舰店", ["张三"], 100, "2026-09-10"),
        ]
        with self.assertLogs("common.public_data.extract_ecom_people",
                             level="WARNING"):
            rows = build_fact_rows(detail_rows, _store_meta({}))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["responsible_person"], "张三")

    def test_detail_without_owners_falls_back_to_target_table(self):
        """明细负责人为空 → 回退用月目标表的当日归属集合（各计整店）。"""
        detail_rows = [
            _detail_row("拼多多店", [], 5000, "2026-09-10", channel="拼多多"),
        ]
        store_meta = _store_meta({"拼多多店": (200000.0, ["孙七", "周八"])})
        rows = build_fact_rows(detail_rows, store_meta)
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["responsible_person"] for row in rows},
                         {"孙七", "周八"})
        for row in rows:
            self.assertEqual(row["sales_amount"], 5000.0)
            self.assertEqual(row["monthly_target"], 200000.0)

    def test_detail_owners_take_precedence_over_target_table(self):
        """明细自带负责人非空时不回退：用明细集合而非目标表集合。"""
        detail_rows = [
            _detail_row("旗舰店", ["张三"], 100, "2026-09-10"),
        ]
        store_meta = _store_meta({"旗舰店": (300000.0, ["李四"])})
        rows = build_fact_rows(detail_rows, store_meta)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["responsible_person"], "张三")

    def test_fallback_target_table_also_empty_skips(self):
        """明细与月目标表都无负责人 → 跳过记日志。"""
        detail_rows = [
            _detail_row("私域店", [], 100, "2026-09-10", channel="私域"),
        ]
        with self.assertLogs("common.public_data.extract_ecom_people",
                             level="WARNING"):
            rows = build_fact_rows(
                detail_rows, _store_meta({"私域店": (100000.0, [])})
            )
        self.assertEqual(rows, [])

    def test_target_null_but_owners_present_rows_with_none_target(self):
        """归属与目标解耦：目标表 target 为 NULL 但 owners 存在 → 成行，
        ``monthly_target`` 落 NULL。"""
        detail_rows = [
            _detail_row("即时零售店", [], 800, "2026-09-10", channel="即时零售"),
        ]
        store_meta = _store_meta({"即时零售店": (None, ["吴九"])})
        rows = build_fact_rows(detail_rows, store_meta)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["responsible_person"], "吴九")
        self.assertEqual(rows[0]["sales_amount"], 800.0)
        self.assertIsNone(rows[0]["monthly_target"])

    def test_incomplete_row_is_skipped(self):
        bad = {"channel": "天猫", "store_name": "旗舰店",
               "business_date": "2026-09-10",
               "responsible_person": _owners_json(["张三"])}  # 缺销售额
        with self.assertLogs("common.public_data.extract_ecom_people",
                             level="WARNING"):
            rows = build_fact_rows([bad], _store_meta({}))
        self.assertEqual(rows, [])

    def test_date_object_business_date(self):
        """pymysql 对 DATE 列返回 datetime.date，同样能展开。"""
        row = _detail_row("旗舰店", ["张三"], 100, "2026-09-10")
        row["business_date"] = datetime(2026, 9, 10).date()
        rows = build_fact_rows([row], _store_meta({}))
        self.assertEqual(rows[0]["business_date"], "2026-09-10")

    def test_source_record_id_format(self):
        rows = build_fact_rows(
            [_detail_row("微商城", ["赵六"], 1, "2026-09-01", channel="私域")],
            _store_meta({}),
        )
        self.assertEqual(rows[0]["source_record_id"], "ecom:赵六:2026-09-01")
        self.assertTrue(rows[0]["source_record_id"].startswith("ecom:"))

    def test_multi_store_owner_aggregates_to_one_row(self):
        """同一 owner 负责 2 店（不同渠道）→ 1 行：日销求和、目标求和、渠道 "/" 连接。"""
        detail_rows = [
            _detail_row("旗舰店", ["张三"], 10000, "2026-09-10", channel="天猫"),
            _detail_row("专营店", ["张三"], 2800, "2026-09-10", channel="京东"),
        ]
        store_meta = _store_meta({
            "旗舰店": (300000.0, ["张三"]),
            "专营店": (150000.0, ["张三"]),
        })
        rows = build_fact_rows(detail_rows, store_meta)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["source_record_id"], "ecom:张三:2026-09-10")
        self.assertEqual(row["sales_amount"], 12800.0)
        self.assertEqual(row["monthly_target"], 450000.0)
        self.assertEqual(row["department"], "天猫/京东")

    def test_multi_store_target_counts_each_store_once(self):
        """月目标按不同店铺只计一次：同店多日行不重复累加目标。"""
        detail_rows = [
            _detail_row("旗舰店", ["张三"], 100, "2026-09-10"),
            _detail_row("旗舰店", ["张三"], 200, "2026-09-11"),
            _detail_row("专营店", ["张三"], 50, "2026-09-11"),
        ]
        store_meta = _store_meta({
            "旗舰店": (300000.0, ["张三"]),
            "专营店": (150000.0, ["张三"]),
        })
        rows = build_fact_rows(detail_rows, store_meta)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["monthly_target"], 450000.0)
        by_day = {row["business_date"]: row for row in rows}
        self.assertEqual(by_day["2026-09-10"]["sales_amount"], 100.0)
        self.assertEqual(by_day["2026-09-11"]["sales_amount"], 250.0)


class StoreMetaTests(unittest.TestCase):

    def test_maps_store_to_target_and_owners(self):
        meta = store_meta_by_store([
            _target_row("旗舰店", 300000, owners=["张三", "李四"]),
            _target_row("专营店", "150000.00", owners=["王五"]),
        ])
        self.assertEqual(meta, {
            "旗舰店": {"target": 300000.0, "owners": ["张三", "李四"]},
            "专营店": {"target": 150000.0, "owners": ["王五"]},
        })

    def test_target_null_keeps_owners(self):
        """target 为 NULL 但 owners 存在 → 归属保留（归属与目标解耦）。"""
        meta = store_meta_by_store([
            _target_row("即时零售店", None, owners=["吴九"]),
        ])
        self.assertEqual(
            meta, {"即时零售店": {"target": None, "owners": ["吴九"]}}
        )

    def test_duplicate_store_last_wins_with_warning(self):
        with self.assertLogs("common.public_data.extract_ecom_people",
                             level="WARNING"):
            meta = store_meta_by_store([
                _target_row("旗舰店", 300000, owners=["张三"]),
                _target_row("旗舰店", 400000, channel="京东", owners=["李四"]),
            ])
        self.assertEqual(meta, {"旗舰店": {"target": 400000.0, "owners": ["李四"]}})


# ---------------------------------------------------------------------------
# raw 读取（SQL 过滤语义）
# ---------------------------------------------------------------------------

class FetchSourceRowsTests(unittest.TestCase):

    def test_reads_detail_and_target_tables(self):
        conn = _RawConnection(
            [_detail_row("旗舰店", ["张三"], 100, "2026-09-10")],
            [_target_row("旗舰店", 300000)],
        )
        detail_rows, target_rows = fetch_source_rows(conn, month=9)
        self.assertEqual(detail_rows, conn.detail_rows)
        self.assertEqual(target_rows, conn.target_rows)
        detail_sql, detail_params = conn.executed[0]
        self.assertIn("channel_daily_sales", detail_sql)
        self.assertIn("MONTH(`business_date`)", detail_sql)
        self.assertEqual(detail_params, (9,))
        target_sql, _ = conn.executed[1]
        self.assertIn("channel_monthly_target", target_sql)

    def test_day_filter_takes_precedence_over_month(self):
        conn = _RawConnection([], [])
        fetch_source_rows(conn, month=9, day="2026-09-10")
        detail_sql, detail_params = conn.executed[0]
        self.assertIn("`business_date` = %s", detail_sql)
        self.assertEqual(detail_params, ("2026-09-10",))


# ---------------------------------------------------------------------------
# upsert（fake 内存表，验证业务键幂等）
# ---------------------------------------------------------------------------

class UpsertRowsTests(unittest.TestCase):

    def _rows(self, sales=12800):
        return build_fact_rows(
            [_detail_row("旗舰店", ["张三", "李四"], sales, "2026-09-10")],
            _store_meta({"旗舰店": (300000.0, ["张三", "李四"])}),
        )

    def test_insert_new_rows(self):
        conn = _FakeConnection()
        stats = upsert_rows(conn, self._rows(), _NOW)
        self.assertEqual(stats, {"inserted": 2, "updated": 0})
        self.assertEqual(len(conn.table), 2)
        row = conn.table["ecom:张三:2026-09-10"]
        self.assertEqual(row["region"], "qudao")
        self.assertEqual(row["department"], "天猫")
        self.assertEqual(row["sales_amount"], 12800.0)
        self.assertEqual(row["monthly_target"], 300000.0)
        self.assertEqual(row["sync_run_id"], ECOM_RUN_ID)
        self.assertNotEqual(row["sync_run_id"],
                            "00000000-0000-0000-0000-000000000000")

    def test_replay_is_idempotent_and_updates_values(self):
        """同 (region, owner, day) 二次执行：行数不变、值更新、走 UPDATE。"""
        conn = _FakeConnection()
        upsert_rows(conn, self._rows(sales=12800), _NOW)
        stats = upsert_rows(conn, self._rows(sales=20000), _NOW)
        self.assertEqual(stats, {"inserted": 0, "updated": 2})
        self.assertEqual(len(conn.table), 2)
        self.assertEqual(conn.insert_count(), 2)   # 仅第一轮
        self.assertEqual(conn.update_count(), 2)   # 第二轮全部 UPDATE
        self.assertEqual(
            conn.table["ecom:张三:2026-09-10"]["sales_amount"], 20000.0
        )

    def test_existing_business_key_row_is_updated_in_place(self):
        """业务键已存在（PK 不同）时保留其 PK 就地更新，不插新行。"""
        conn = _FakeConnection(table={
            "stream:qudao:u-zhangsan:2026-09-10": {
                "source_record_id": "stream:qudao:u-zhangsan:2026-09-10",
                "region": "qudao",
                "responsible_person": "张三",
                "business_date": "2026-09-10",
                "sales_amount": 1.0,
                "monthly_target": None,
            },
        })
        stats = upsert_rows(conn, self._rows(), _NOW)
        self.assertEqual(stats, {"inserted": 1, "updated": 1})
        self.assertEqual(len(conn.table), 2)  # 李四新插，张三就地更新
        row = conn.table["stream:qudao:u-zhangsan:2026-09-10"]
        self.assertEqual(row["sales_amount"], 12800.0)
        self.assertEqual(row["monthly_target"], 300000.0)


if __name__ == "__main__":
    unittest.main()
