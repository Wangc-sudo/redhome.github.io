import hashlib
import json
import unittest
from unittest.mock import patch

from common.public_data.dingtalk_read import (
    DingTalkReadError,
    DingTalkReadGateway,
    DingTalkSchemaSnapshot,
)
from common.public_data.manifest import DingTalkSheet, FieldMapping


def _sheet():
    return DingTalkSheet(
        base_id="base-1",
        sheet_id="sheet-1",
        sheet_name="店铺扣点费用管理",
        dataset="finance_store_commission",
        target_table="fin_store_commission",
        max_pages=2,
        fields=(
            FieldMapping("公司主体", "company_entity", "text"),
            FieldMapping("费用项目", "fee_item", "text"),
        ),
    )


class DingTalkReadGatewayTests(unittest.TestCase):
    def _gateway(self, responses, requests=None, operator_id="operator-id"):
        response_iterator = iter(responses)

        def request_json(url, method="GET", body=None, headers=None):
            if requests is not None:
                requests.append((url, method, body, headers))
            return next(response_iterator)

        return DingTalkReadGateway(
            app_key="app-key",
            app_secret="app-secret",
            operator_id=operator_id,
            request_json=request_json,
        )

    def test_validates_schema_and_reads_records_using_get_requests(self):
        requests = []
        gateway = self._gateway(
            [
                {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]},
                {
                    "fields": [
                        {"name": "公司主体", "type": "text"},
                        {"name": "费用项目", "type": "text"},
                    ]
                },
                {"records": [{"id": "record-1"}], "hasMore": False},
            ],
            requests,
        )

        snapshot = gateway.validate_sheet(_sheet())
        records = gateway.read_records(_sheet())

        self.assertIsInstance(snapshot, DingTalkSchemaSnapshot)
        self.assertEqual("base-1", snapshot.base_id)
        self.assertEqual("sheet-1", snapshot.sheet_id)
        self.assertEqual([{"id": "record-1"}], records)
        self.assertEqual(
            {"list_fields", "list_sheets", "read_records", "validate_sheet"},
            {
                name
                for name, value in vars(DingTalkReadGateway).items()
                if callable(value) and not name.startswith("_")
            },
        )
        self.assertFalse(hasattr(gateway, "update_records"))
        self.assertFalse(hasattr(gateway, "send_group_markdown"))
        self.assertTrue(all(method == "GET" and body is None for _, method, body, _ in requests))
        self.assertTrue(
            all(
                "x-acs-dingtalk-access-token" in headers
                for _, _, _, headers in requests
            )
        )

    def test_encodes_base_and_sheet_ids_as_url_path_segments(self):
        requests = []
        gateway = self._gateway([{"value": []}, {"fields": []}], requests)

        self.assertEqual([], gateway.list_sheets("base /?#"))
        self.assertEqual([], gateway.list_fields("base /?#", "sheet /?#"))

        self.assertEqual(
            [
                "https://api.dingtalk.com/v1.0/notable/bases/base%20%2F%3F%23/sheets?operatorId=operator-id",
                "https://api.dingtalk.com/v1.0/notable/bases/base%20%2F%3F%23/sheets/sheet%20%2F%3F%23/fields?operatorId=operator-id",
            ],
            [url for url, _, _, _ in requests],
        )

    def test_rejects_invalid_identifiers_before_request_without_leaks(self):
        cases = {
            "base_id": (
                lambda gateway, value: gateway.list_sheets(value),
                (None, "", 42),
            ),
            "field_base_id": (
                lambda gateway, value: gateway.list_fields(value, "sheet-id"),
                (None, "", 42),
            ),
            "sheet_id": (
                lambda gateway, value: gateway.list_fields("base-id", value),
                (None, "", 42),
            ),
            "operator_id": (
                lambda gateway, value: gateway.list_sheets("base-id"),
                (None, "", 42),
            ),
        }

        for name, (request, invalid_values) in cases.items():
            for invalid_value in invalid_values:
                with self.subTest(name=name, invalid_value=invalid_value):
                    requests = []
                    gateway = self._gateway(
                        [{"value": []}],
                        requests,
                        operator_id=(
                            invalid_value if name == "operator_id" else "operator-id"
                        ),
                    )

                    with self.assertRaisesRegex(
                        DingTalkReadError, r"^DingTalk read request failed$"
                    ) as raised:
                        request(gateway, invalid_value)

                    self.assertEqual([], requests)
                    self.assertNotIn("base-id", str(raised.exception))
                    self.assertNotIn("operator-id", str(raised.exception))

    def test_validate_sheet_rejects_invalid_sheet_id_before_request(self):
        configured_sheet = _sheet()
        invalid_sheet = DingTalkSheet(
            base_id=configured_sheet.base_id,
            sheet_id="",
            sheet_name=configured_sheet.sheet_name,
            dataset=configured_sheet.dataset,
            target_table=configured_sheet.target_table,
            max_pages=configured_sheet.max_pages,
            fields=configured_sheet.fields,
        )
        requests = []
        gateway = self._gateway([], requests)

        with self.assertRaises(DingTalkReadError):
            gateway.validate_sheet(invalid_sheet)

        self.assertEqual([], requests)

    def test_rejects_invalid_pagination_and_record_ids(self):
        cases = {
            "missing_next_token": (
                [{"records": [{"id": "record-1"}], "hasMore": True}],
                "nextToken",
            ),
            "max_pages": (
                [
                    {
                        "records": [{"id": "record-1"}],
                        "hasMore": True,
                        "nextToken": "page-2",
                    },
                    {
                        "records": [{"id": "record-2"}],
                        "hasMore": True,
                        "nextToken": "page-3",
                    },
                ],
                "max_pages",
            ),
            "duplicate": (
                [
                    {
                        "records": [{"id": "record-1"}, {"id": "record-1"}],
                        "hasMore": False,
                    }
                ],
                "duplicate",
            ),
            "empty_record_id": (
                [{"records": [{"id": ""}], "hasMore": False}],
                "record id",
            ),
            "missing_has_more": (
                [{"records": [{"id": "record-1"}]}],
                "hasMore",
            ),
            "invalid_has_more": (
                [{"records": [{"id": "record-1"}], "hasMore": None}],
                "hasMore",
            ),
            "string_has_more": (
                [{"records": [{"id": "record-1"}], "hasMore": "false"}],
                "hasMore",
            ),
        }

        for name, (responses, error_text) in cases.items():
            with self.subTest(name=name):
                gateway = self._gateway(responses)

                with self.assertRaisesRegex(DingTalkReadError, error_text):
                    gateway.read_records(_sheet())

    def test_rejects_invalid_record_input_and_response_shapes_before_partial_return(self):
        requests = []
        gateway = self._gateway([], requests)
        with self.assertRaisesRegex(DingTalkReadError, "page_size"):
            gateway.read_records(_sheet(), page_size=0)
        self.assertEqual([], requests)

        sheet = _sheet()
        invalid_page_sheet = DingTalkSheet(
            base_id=sheet.base_id,
            sheet_id=sheet.sheet_id,
            sheet_name=sheet.sheet_name,
            dataset=sheet.dataset,
            target_table=sheet.target_table,
            max_pages=0,
            fields=sheet.fields,
        )
        with self.assertRaisesRegex(DingTalkReadError, "max_pages"):
            gateway.read_records(invalid_page_sheet)
        self.assertEqual([], requests)

        invalid_id_sheets = {
            "base_id": DingTalkSheet(
                base_id="",
                sheet_id=sheet.sheet_id,
                sheet_name=sheet.sheet_name,
                dataset=sheet.dataset,
                target_table=sheet.target_table,
                max_pages=sheet.max_pages,
                fields=sheet.fields,
            ),
            "sheet_id": DingTalkSheet(
                base_id=sheet.base_id,
                sheet_id="",
                sheet_name=sheet.sheet_name,
                dataset=sheet.dataset,
                target_table=sheet.target_table,
                max_pages=sheet.max_pages,
                fields=sheet.fields,
            ),
        }
        for name, invalid_id_sheet in invalid_id_sheets.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(
                    DingTalkReadError,
                    r"^finance_store_commission: record request$",
                ):
                    gateway.read_records(invalid_id_sheet)
        self.assertEqual([], requests)

        cases = {
            "both_record_keys": [{"records": [], "value": [], "hasMore": False}],
            "neither_record_key": [{"hasMore": False}],
            "non_mapping_record": [{"records": ["record-1"], "hasMore": False}],
            "non_string_record_id": [
                {"records": [{"id": 1}], "hasMore": False}
            ],
            "cross_page_duplicate": [
                {
                    "records": [{"id": "record-1"}],
                    "hasMore": True,
                    "nextToken": "page-2",
                },
                {"records": [{"id": "record-1"}], "hasMore": False},
            ],
        }
        expected_errors = {
            "both_record_keys": "record response",
            "neither_record_key": "record response",
            "non_mapping_record": "record mapping",
            "non_string_record_id": "record id",
            "cross_page_duplicate": "duplicate record id",
        }
        for name, responses in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(DingTalkReadError, expected_errors[name]):
                    self._gateway(responses).read_records(_sheet())

    def test_reads_value_records_and_allows_final_page_at_max_pages(self):
        requests = []
        records = self._gateway(
            [
                {
                    "records": [{"id": "record-1"}],
                    "hasMore": True,
                    "nextToken": "page-2",
                },
                {"value": [{"id": "record-2"}], "hasMore": False},
            ],
            requests,
        ).read_records(_sheet())

        self.assertEqual(["record-1", "record-2"], [record["id"] for record in records])
        self.assertEqual(
            [
                "https://api.dingtalk.com/v1.0/notable/bases/base-1/sheets/sheet-1/records?operatorId=operator-id&pageSize=100",
                "https://api.dingtalk.com/v1.0/notable/bases/base-1/sheets/sheet-1/records?operatorId=operator-id&pageSize=100&nextToken=page-2",
            ],
            [url for url, _, _, _ in requests],
        )

    def test_rejects_missing_or_empty_oauth_access_token_without_leaks(self):
        for name, token_response in {
            "missing": {},
            "empty": {"accessToken": ""},
        }.items():
            with self.subTest(name=name):
                with patch(
                    "common.public_data.dingtalk_read._http_json",
                    return_value=token_response,
                ) as request_json:
                    gateway = DingTalkReadGateway(
                        app_key="app-key",
                        app_secret="app-secret",
                        operator_id="operator-id",
                    )
                    with self.assertRaises(DingTalkReadError) as raised:
                        gateway.list_sheets("base-1")

                error_text = str(raised.exception)
                self.assertRegex(
                    error_text, r"^DingTalk read (?:token|request) failed$"
                )
                self.assertNotIn("app-key", error_text)
                self.assertNotIn("app-secret", error_text)
                self.assertNotIn("https://", error_text)
                self.assertNotIn(str(token_response), error_text)
                request_json.assert_called_once()

    def test_rejects_renamed_sheet_before_loading_fields(self):
        requests = []
        gateway = self._gateway(
            [{"value": [{"id": "sheet-1", "name": "renamed"}]}], requests
        )

        with self.assertRaisesRegex(DingTalkReadError, "sheet_name"):
            gateway.validate_sheet(_sheet())

        self.assertEqual(1, len(requests))

    def test_rejects_missing_or_duplicate_sheet_id_before_loading_fields(self):
        cases = {
            "missing": [],
            "duplicate": [
                {"id": "sheet-1", "name": "店铺扣点费用管理"},
                {"id": "sheet-1", "name": "店铺扣点费用管理"},
            ],
        }

        for name, sheets in cases.items():
            with self.subTest(name=name):
                requests = []
                gateway = self._gateway([{"value": sheets}], requests)

                with self.assertRaisesRegex(DingTalkReadError, "sheet id"):
                    gateway.validate_sheet(_sheet())

                self.assertEqual(1, len(requests))

    def test_rejects_field_schema_drift_and_canonicalizes_field_hash(self):
        field_schemas = {
            "extra": [
                {"name": "公司主体", "type": "text"},
                {"name": "费用项目", "type": "text"},
                {"name": "额外字段", "type": "text"},
            ],
            "missing": [{"name": "公司主体", "type": "text"}],
            "type_changed": [
                {"name": "公司主体", "type": "number"},
                {"name": "费用项目", "type": "text"},
            ],
            "duplicate": [
                {"name": "公司主体", "type": "text"},
                {"name": "费用项目", "type": "text"},
                {"name": "公司主体", "type": "text"},
            ],
        }
        sheet_response = {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]}

        for name, fields in field_schemas.items():
            with self.subTest(name=name):
                gateway = self._gateway([sheet_response, {"value": fields}])

                with self.assertRaisesRegex(DingTalkReadError, "field schema"):
                    gateway.validate_sheet(_sheet())

        fields = [
            {"name": "费用项目", "type": "text"},
            {"name": "公司主体", "type": "text"},
        ]
        first_snapshot = self._gateway(
            [sheet_response, {"fields": fields}]
        ).validate_sheet(_sheet())
        second_snapshot = self._gateway(
            [sheet_response, {"fields": list(reversed(fields))}]
        ).validate_sheet(_sheet())
        expected_json = json.dumps(
            [
                {"name": "公司主体", "type": "text"},
                {"name": "费用项目", "type": "text"},
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

        self.assertEqual(expected_json, first_snapshot.fields_json)
        self.assertEqual(first_snapshot.fields_json, second_snapshot.fields_json)
        self.assertEqual(first_snapshot.fields_sha256, second_snapshot.fields_sha256)
        self.assertEqual(
            hashlib.sha256(expected_json.encode("utf-8")).hexdigest(),
            first_snapshot.fields_sha256,
        )

    def test_rejects_invalid_configured_and_remote_field_names(self):
        sheet = _sheet()
        duplicate_name_sheet = DingTalkSheet(
            base_id=sheet.base_id,
            sheet_id=sheet.sheet_id,
            sheet_name=sheet.sheet_name,
            dataset=sheet.dataset,
            target_table=sheet.target_table,
            max_pages=sheet.max_pages,
            fields=sheet.fields
            + (FieldMapping("公司主体", "company_entity_number", "number"),),
        )
        invalid_cases = {
            "duplicate_configured_name": (
                duplicate_name_sheet,
                [
                    {"name": "公司主体", "type": "text"},
                    {"name": "公司主体", "type": "number"},
                    {"name": "费用项目", "type": "text"},
                ],
            ),
            "empty_configured_name": (
                DingTalkSheet(
                    base_id=sheet.base_id,
                    sheet_id=sheet.sheet_id,
                    sheet_name=sheet.sheet_name,
                    dataset=sheet.dataset,
                    target_table=sheet.target_table,
                    max_pages=sheet.max_pages,
                    fields=(FieldMapping("", "empty_name", "text"),),
                ),
                [{"name": "", "type": "text"}],
            ),
            "malformed_configured_mapping": (
                DingTalkSheet(
                    base_id=sheet.base_id,
                    sheet_id=sheet.sheet_id,
                    sheet_name=sheet.sheet_name,
                    dataset=sheet.dataset,
                    target_table=sheet.target_table,
                    max_pages=sheet.max_pages,
                    fields=(object(),),
                ),
                [{"name": "公司主体", "type": "text"}],
            ),
        }
        sheet_response = {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]}

        for name, (invalid_sheet, fields) in invalid_cases.items():
            with self.subTest(name=name):
                gateway = self._gateway([sheet_response, {"fields": fields}])

                with self.assertRaisesRegex(DingTalkReadError, "field schema"):
                    gateway.validate_sheet(invalid_sheet)

    def test_rejects_malformed_remote_field_schema(self):
        sheet_response = {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]}
        cases = {
            "non_mapping": ["not-a-mapping"],
            "missing_name": [{"type": "text"}],
            "empty_name": [{"name": "", "type": "text"}],
            "non_string_type": [{"name": "公司主体", "type": 1}],
            "empty_type": [{"name": "公司主体", "type": ""}],
        }

        for name, fields in cases.items():
            with self.subTest(name=name):
                gateway = self._gateway([sheet_response, {"fields": fields}])

                with self.assertRaisesRegex(DingTalkReadError, "field schema"):
                    gateway.validate_sheet(_sheet())

    def test_translates_registered_field_types_and_rejects_unregistered_types(self):
        sheet = _sheet()
        currency_sheet = DingTalkSheet(
            base_id=sheet.base_id,
            sheet_id=sheet.sheet_id,
            sheet_name=sheet.sheet_name,
            dataset=sheet.dataset,
            target_table=sheet.target_table,
            max_pages=sheet.max_pages,
            fields=(FieldMapping("金额", "amount", "currency"),),
        )
        sheet_response = {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]}
        snapshot = self._gateway(
            [sheet_response, {"fields": [{"name": "金额", "type": "currency"}]}]
        ).validate_sheet(currency_sheet)
        self.assertEqual('[{"name":"金额","type":"currency"}]', snapshot.fields_json)

        unsupported_type_sheet = DingTalkSheet(
            base_id=sheet.base_id,
            sheet_id=sheet.sheet_id,
            sheet_name=sheet.sheet_name,
            dataset=sheet.dataset,
            target_table=sheet.target_table,
            max_pages=sheet.max_pages,
            fields=(FieldMapping("金额", "amount", "unsupported"),),
        )
        gateway = self._gateway(
            [sheet_response, {"fields": [{"name": "金额", "type": "unsupported"}]}]
        )
        with self.assertRaisesRegex(DingTalkReadError, "field schema"):
            gateway.validate_sheet(unsupported_type_sheet)

    def test_rejects_duplicate_configured_field_schema(self):
        sheet = _sheet()
        duplicate_field_sheet = DingTalkSheet(
            base_id=sheet.base_id,
            sheet_id=sheet.sheet_id,
            sheet_name=sheet.sheet_name,
            dataset=sheet.dataset,
            target_table=sheet.target_table,
            max_pages=sheet.max_pages,
            fields=sheet.fields + (sheet.fields[0],),
        )
        gateway = self._gateway(
            [
                {"value": [{"id": "sheet-1", "name": "店铺扣点费用管理"}]},
                {
                    "fields": [
                        {"name": "公司主体", "type": "text"},
                        {"name": "费用项目", "type": "text"},
                    ]
                },
            ]
        )

        with self.assertRaisesRegex(DingTalkReadError, "field schema"):
            gateway.validate_sheet(duplicate_field_sheet)

    def test_rejects_paginated_sheet_and_field_lists(self):
        cases = {
            "sheet_has_more": (
                lambda gateway: gateway.list_sheets("base-1"),
                {"value": [], "hasMore": True},
                "sheet list",
            ),
            "sheet_next_token": (
                lambda gateway: gateway.list_sheets("base-1"),
                {"value": [], "nextToken": "page-2"},
                "sheet list",
            ),
            "field_has_more": (
                lambda gateway: gateway.list_fields("base-1", "sheet-1"),
                {"fields": [], "hasMore": True},
                "field list",
            ),
            "sheet_string_has_more": (
                lambda gateway: gateway.list_sheets("base-1"),
                {"value": [], "hasMore": "true"},
                "sheet list",
            ),
            "field_string_has_more": (
                lambda gateway: gateway.list_fields("base-1", "sheet-1"),
                {"fields": [], "hasMore": "false"},
                "field list",
            ),
            "field_next_token": (
                lambda gateway: gateway.list_fields("base-1", "sheet-1"),
                {"fields": [], "nextToken": "page-2"},
                "field list",
            ),
        }

        for name, (request, response, error_text) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(DingTalkReadError, error_text):
                    request(self._gateway([response]))

        self.assertEqual(
            [], self._gateway([{"value": [], "hasMore": False}]).list_sheets("base-1")
        )
        self.assertEqual(
            [], self._gateway([{"fields": []}]).list_fields("base-1", "sheet-1")
        )

    def test_caches_default_oauth_token_for_consecutive_notable_gets(self):
        with patch(
            "common.public_data.dingtalk_read._http_json",
            side_effect=[
                {"accessToken": "cached-token"},
                {"value": []},
                {"value": []},
            ],
        ) as request_json:
            gateway = DingTalkReadGateway(
                app_key="app-key",
                app_secret="app-secret",
                operator_id="operator-id",
            )

            self.assertEqual([], gateway.list_sheets("base-1"))
            self.assertEqual([], gateway.list_sheets("base-1"))

        oauth_calls = [
            call for call in request_json.call_args_list if call.kwargs["method"] == "POST"
        ]
        notable_calls = [
            call for call in request_json.call_args_list if call.kwargs["method"] == "GET"
        ]
        self.assertEqual(1, len(oauth_calls))
        self.assertEqual(
            "https://api.dingtalk.com/v1.0/oauth2/accessToken",
            oauth_calls[0].args[0],
        )
        self.assertEqual(2, len(notable_calls))
        self.assertEqual(
            {"appKey": "app-key", "appSecret": "app-secret"},
            oauth_calls[0].kwargs["body"],
        )
        self.assertTrue(all(call.kwargs["body"] is None for call in notable_calls))
        self.assertEqual(
            ["cached-token", "cached-token"],
            [
                call.kwargs["headers"]["x-acs-dingtalk-access-token"]
                for call in notable_calls
            ],
        )
        self.assertEqual(
            [
                "https://api.dingtalk.com/v1.0/notable/bases/base-1/sheets?operatorId=operator-id",
                "https://api.dingtalk.com/v1.0/notable/bases/base-1/sheets?operatorId=operator-id",
            ],
            [call.args[0] for call in notable_calls],
        )

    def test_refreshes_default_oauth_token_after_cache_expiry(self):
        with (
            patch(
                "common.public_data.dingtalk_read._http_json",
                side_effect=[
                    {"accessToken": "first-token"},
                    {"value": []},
                    {"accessToken": "second-token"},
                    {"value": []},
                ],
            ) as request_json,
            patch(
                "common.public_data.dingtalk_read.time.time",
                side_effect=[1000.0, 7001.0, 7001.0],
            ),
        ):
            gateway = DingTalkReadGateway("app-key", "app-secret", "operator-id")

            self.assertEqual([], gateway.list_sheets("base-1"))
            self.assertEqual([], gateway.list_sheets("base-1"))

        oauth_calls = [
            call for call in request_json.call_args_list if call.kwargs["method"] == "POST"
        ]
        notable_calls = [
            call for call in request_json.call_args_list if call.kwargs["method"] == "GET"
        ]
        self.assertEqual(2, len(oauth_calls))
        self.assertEqual(
            ["first-token", "second-token"],
            [
                call.kwargs["headers"]["x-acs-dingtalk-access-token"]
                for call in notable_calls
            ],
        )

    def test_sanitizes_default_oauth_transport_failure(self):
        with patch(
            "common.public_data.dingtalk_read._http_json",
            side_effect=RuntimeError("app-key app-secret https://api.dingtalk.com"),
        ):
            gateway = DingTalkReadGateway("app-key", "app-secret", "operator-id")
            with self.assertRaisesRegex(
                DingTalkReadError, r"^DingTalk read request failed$"
            ) as raised:
                gateway.list_sheets("base-1")

        error_text = str(raised.exception)
        self.assertNotIn("app-key", error_text)
        self.assertNotIn("app-secret", error_text)
        self.assertNotIn("https://", error_text)


if __name__ == "__main__":
    unittest.main()
