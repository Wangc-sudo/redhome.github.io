import unittest
from decimal import Decimal

from common.public_data.cleaning import (
    RuleConfigurationError,
    evaluate_rows,
    issue_fingerprint,
    mask_value,
    rule_version,
    validate_rule,
)


def _rule(rule_type, **kwargs):
    return {
        "dataset": "channel_daily_sales",
        "rule_code": f"TEST_{rule_type.upper()}",
        "rule_type": rule_type,
        "severity": "error",
        "params": {},
        **kwargs,
    }


class CleaningTests(unittest.TestCase):
    def test_required_preserves_zero_and_negative_numbers(self):
        result = evaluate_rows(
            "channel_daily_sales",
            [{"source_record_id": "a", "sales_amount": Decimal("0")},
             {"source_record_id": "b", "sales_amount": Decimal("-2.50")}],
            [_rule("required", field_name="sales_amount"),
             _rule("range", field_name="sales_amount", params={"min": -10, "max": 10})],
        )
        self.assertEqual(2, result.records_passed)
        self.assertEqual([], result.issues)
        self.assertEqual(Decimal("0"), result.rows[0]["sales_amount"])

    def test_range_uses_decimal_and_rejects_non_finite_values(self):
        result = evaluate_rows(
            "channel_daily_sales",
            [{"source_record_id": "a", "sales_amount": "0.10"},
             {"source_record_id": "b", "sales_amount": "NaN"}],
            [_rule("range", field_name="sales_amount", params={"min": "0.10", "max": "0.20"})],
        )
        self.assertEqual(["invalid_decimal"], [i.reason for i in result.issues])
        self.assertEqual(1, result.records_passed)

    def test_required_does_not_confuse_false_with_missing(self):
        result = evaluate_rows(
            "channel_daily_sales", [{"source_record_id": "a", "flag": False}],
            [_rule("required", field_name="flag")],
        )
        self.assertEqual(1, result.records_passed)

    def test_unique_distinguishes_exact_and_conflict(self):
        rows = [
            {"source_record_id": "a", "channel": "抖音", "store_name": "甲", "business_date": "2026-10-26"},
            {"source_record_id": "a", "channel": "抖音", "store_name": "甲", "business_date": "2026-10-26"},
            {"source_record_id": "b", "channel": "抖音", "store_name": "甲", "business_date": "2026-10-26"},
        ]
        result = evaluate_rows(
            "channel_daily_sales", rows,
            [_rule("unique", params={"fields": ["channel", "store_name", "business_date"]})],
        )
        self.assertEqual(["duplicate_exact", "duplicate_conflict"], [i.reason for i in result.issues])
        self.assertEqual(["warning", "error"], [i.severity for i in result.issues])
        self.assertEqual(2, result.duplicates)

    def test_unique_state_can_span_batches(self):
        rule = _rule("unique", params={"fields": ["channel", "store_name", "business_date"]})
        state = {}
        first = {"source_record_id": "a", "channel": "抖音", "store_name": "甲", "business_date": "2026-10-26"}
        second = dict(first, source_record_id="b")
        evaluate_rows("channel_daily_sales", [first], [rule], state=state)
        result = evaluate_rows("channel_daily_sales", [second], [rule], state=state)
        self.assertEqual(["duplicate_conflict"], [i.reason for i in result.issues])

    def test_override_applies_before_rules_and_is_auditable_in_issue(self):
        overrides = {("a", "channel"): "抖音"}
        result = evaluate_rows(
            "channel_daily_sales",
            [{"source_record_id": "a", "channel": " 抖 音 ", "store_name": "甲"}],
            [_rule("enum", field_name="channel", params={"values": ["抖音", "京东"]})],
            overrides=overrides,
        )
        self.assertEqual("抖音", result.rows[0]["channel"])
        self.assertEqual([], result.issues)

    def test_override_can_supply_a_missing_required_field(self):
        result = evaluate_rows(
            "channel_daily_sales", [{"source_record_id": "a"}],
            [_rule("required", field_name="channel")],
            overrides={("a", "channel"): "抖音"},
        )
        self.assertEqual(1, result.records_passed)
        self.assertEqual("抖音", result.rows[0]["channel"])

    def test_mapping_does_not_treat_missing_as_unmapped(self):
        result = evaluate_rows(
            "channel_daily_sales", [{"source_record_id": "a", "channel": None}],
            [_rule("mapping", field_name="channel", params={"values": {"抖音旗舰店": "抖音"}})],
        )
        self.assertEqual(0, result.unmapped)
        self.assertEqual([], result.issues)

    def test_sensitive_values_are_masked_and_payload_is_not_persisted(self):
        issue = evaluate_rows(
            "wdt_order_line_fact",
            [{"source_record_id": "a", "customer_id": "13800138000", "payload_json": "not-json"}],
            [_rule("schema", field_name="payload_json", params={"format": "json_object"}),
             _rule("required", field_name="customer_id")],
        ).issues[0].as_row("run")
        self.assertEqual("<redacted>", issue["original_value"])
        self.assertEqual("13*******00", mask_value("customer_id", "13800138000"))

    def test_rule_validation_rejects_executable_or_unknown_rules(self):
        with self.assertRaises(RuleConfigurationError):
            validate_rule(_rule("python", field_name="x"))
        with self.assertRaises(RuleConfigurationError):
            validate_rule(_rule("regex", field_name="x", params={"pattern": "["}))
        with self.assertRaises(RuleConfigurationError):
            validate_rule(_rule("unique", params={"fields": []}))

    def test_rule_version_and_issue_fingerprint_are_stable(self):
        rules = [_rule("required", field_name="channel", id=1, version=2)]
        self.assertEqual(rule_version(rules), rule_version([dict(rules[0])]))
        issue = {"dataset": "d", "source_record_id": "s", "business_key": "b",
                 "rule_code": "R", "field_name": "f", "reason": "required"}
        self.assertEqual(issue_fingerprint(issue), issue_fingerprint(dict(issue)))


if __name__ == "__main__":
    unittest.main()
