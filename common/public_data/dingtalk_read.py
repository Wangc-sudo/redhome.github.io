import hashlib
import json
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from common.dingtalk.client import _http_json


_TOKEN_CACHE_SECONDS = 100 * 60
_DINGTALK_SOURCE_TYPES = {
    "text": "text",
    "singleSelect": "singleSelect",
    "number": "number",
    "currency": "currency",
    "date": "date",
    "user": "user",
    "multipleSelect": "multipleSelect",
    "unidirectionalLink": "unidirectionalLink",
}


class DingTalkReadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DingTalkSchemaSnapshot:
    base_id: str
    sheet_id: str
    fields_sha256: str
    fields_json: str


class DingTalkReadGateway:
    def __init__(self, app_key, app_secret, operator_id, request_json=None):
        self._app_key = app_key
        self._app_secret = app_secret
        self._operator_id = operator_id
        self._request_json = _http_json if request_json is None else request_json
        self._token_provider = (
            self._request_access_token
            if request_json is None
            else self._test_access_token
        )
        self._token = None
        self._token_timestamp = 0.0

    def list_sheets(self, base_id):
        base_id = self._path_segment(base_id)
        response = self._notable_request(
            f"/v1.0/notable/bases/{base_id}/sheets",
            {"operatorId": self._operator_id},
        )
        return self._list_from_response(response, "value", "sheets", "sheet list")

    def list_fields(self, base_id, sheet_id):
        base_id = self._path_segment(base_id)
        sheet_id = self._path_segment(sheet_id)
        response = self._notable_request(
            f"/v1.0/notable/bases/{base_id}/sheets/{sheet_id}/fields",
            {"operatorId": self._operator_id},
        )
        return self._list_from_response(response, "value", "fields", "field list")

    def validate_sheet(self, sheet):
        try:
            self._validate_path_segment(sheet.base_id)
        except DingTalkReadError:
            raise self._failure(sheet.dataset, "sheet list") from None
        try:
            self._validate_path_segment(sheet.sheet_id)
        except DingTalkReadError:
            raise self._failure(sheet.dataset, "sheet id") from None

        try:
            sheets = self.list_sheets(sheet.base_id)
        except DingTalkReadError:
            raise self._failure(sheet.dataset, "sheet list") from None

        matches = [
            remote_sheet
            for remote_sheet in sheets
            if isinstance(remote_sheet, Mapping)
            and remote_sheet.get("id") == sheet.sheet_id
        ]
        if len(matches) != 1:
            raise self._failure(sheet.dataset, "sheet id")
        if matches[0].get("name") != sheet.sheet_name:
            raise self._failure(sheet.dataset, "sheet_name")

        try:
            remote_fields = self.list_fields(sheet.base_id, sheet.sheet_id)
        except DingTalkReadError:
            raise self._failure(sheet.dataset, "field schema") from None

        expected_fields = set()
        expected_names = set()
        for field in sheet.fields:
            source_name = getattr(field, "source_name", None)
            registered_source_type = getattr(field, "source_type", None)
            if (
                not isinstance(source_name, str)
                or not source_name
                or not isinstance(registered_source_type, str)
            ):
                raise self._failure(sheet.dataset, "field schema")
            source_type = _DINGTALK_SOURCE_TYPES.get(registered_source_type)
            if source_type is None or source_name in expected_names:
                raise self._failure(sheet.dataset, "field schema")
            expected_names.add(source_name)
            expected_fields.add((source_name, source_type))

        remote_fields_set = set()
        remote_names = set()
        for field in remote_fields:
            if not isinstance(field, Mapping):
                raise self._failure(sheet.dataset, "field schema")
            name = field.get("name")
            field_type = field.get("type")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(field_type, str)
                or not field_type
                or name in remote_names
            ):
                raise self._failure(sheet.dataset, "field schema")
            remote_names.add(name)
            remote_fields_set.add((name, field_type))

        missing = expected_fields - remote_fields_set
        if missing:
            raise self._failure(sheet.dataset, "field schema")

        fields_json = json.dumps(
            [
                {"name": name, "type": field_type}
                for name, field_type in sorted(remote_fields_set)
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return DingTalkSchemaSnapshot(
            base_id=sheet.base_id,
            sheet_id=sheet.sheet_id,
            fields_sha256=hashlib.sha256(fields_json.encode("utf-8")).hexdigest(),
            fields_json=fields_json,
        )

    def read_records(self, sheet, page_size=100):
        if type(page_size) is not int or not 0 < page_size <= 1000:
            raise self._failure(sheet.dataset, "page_size")
        if type(sheet.max_pages) is not int or sheet.max_pages < 1:
            raise self._failure(sheet.dataset, "max_pages")
        try:
            base_id = self._path_segment(sheet.base_id)
            sheet_id = self._path_segment(sheet.sheet_id)
        except DingTalkReadError:
            raise self._failure(sheet.dataset, "record request") from None

        records = []
        seen_ids = set()
        next_token = None
        page_number = 0
        while True:
            page_number += 1
            query = {
                "operatorId": self._operator_id,
                "pageSize": page_size,
            }
            if next_token is not None:
                query["nextToken"] = next_token
            try:
                response = self._notable_request(
                    f"/v1.0/notable/bases/{base_id}/sheets/{sheet_id}/records",
                    query,
                )
            except DingTalkReadError:
                raise self._failure(sheet.dataset, "record request") from None

            page_records = self._records_from_response(response, sheet.dataset)
            for record in page_records:
                if not isinstance(record, Mapping):
                    raise self._failure(sheet.dataset, "record mapping")
                record_id = record.get("id")
                if not isinstance(record_id, str) or not record_id:
                    raise self._failure(sheet.dataset, "record id")
                if record_id in seen_ids:
                    raise self._failure(sheet.dataset, "duplicate record id")
                seen_ids.add(record_id)
                records.append(record)

            has_more = response.get("hasMore")
            if has_more is False:
                return records
            if has_more is not True:
                raise self._failure(sheet.dataset, "hasMore")
            if page_number >= sheet.max_pages:
                raise self._failure(sheet.dataset, "max_pages")
            candidate_token = response.get("nextToken")
            if not isinstance(candidate_token, str) or not candidate_token:
                raise self._failure(sheet.dataset, "nextToken")
            next_token = candidate_token

    @staticmethod
    def _validate_path_segment(value):
        if not isinstance(value, str) or not value:
            raise DingTalkReadError("DingTalk read request failed")

    @classmethod
    def _path_segment(cls, value):
        cls._validate_path_segment(value)
        return urllib.parse.quote(value, safe="")

    def _notable_request(self, path, query):
        if not isinstance(self._operator_id, str) or not self._operator_id:
            raise DingTalkReadError("DingTalk read request failed")
        url = "https://api.dingtalk.com" + path + "?" + urllib.parse.urlencode(query)
        try:
            return self._request_json(
                url,
                method="GET",
                body=None,
                headers={"x-acs-dingtalk-access-token": self._get_access_token()},
            )
        except Exception:
            raise DingTalkReadError("DingTalk read request failed") from None

    def _get_access_token(self):
        if (
            self._token is not None
            and time.time() - self._token_timestamp < _TOKEN_CACHE_SECONDS
        ):
            return self._token
        token = self._token_provider()
        if not isinstance(token, str) or not token:
            raise DingTalkReadError("DingTalk read token failed")
        self._token = token
        self._token_timestamp = time.time()
        return token

    def _request_access_token(self):
        try:
            response = self._request_json(
                "https://api.dingtalk.com/v1.0/oauth2/accessToken",
                method="POST",
                body={"appKey": self._app_key, "appSecret": self._app_secret},
            )
        except Exception:
            raise DingTalkReadError("DingTalk read token failed") from None
        if not isinstance(response, Mapping):
            raise DingTalkReadError("DingTalk read token failed")
        return response.get("accessToken")

    @staticmethod
    def _test_access_token():
        return "test-access-token"

    @staticmethod
    def _list_from_response(response, first_key, second_key, failure_kind):
        if not isinstance(response, Mapping):
            raise DingTalkReadError(f"DingTalk read {failure_kind} failed")
        if response.get("hasMore", False) is not False or response.get("nextToken"):
            raise DingTalkReadError(f"DingTalk read {failure_kind} failed")
        first_value = response.get(first_key)
        second_value = response.get(second_key)
        list_values = [
            value for value in (first_value, second_value) if isinstance(value, list)
        ]
        if len(list_values) != 1:
            raise DingTalkReadError(f"DingTalk read {failure_kind} failed")
        return list_values[0]

    @staticmethod
    def _records_from_response(response, dataset):
        if not isinstance(response, Mapping):
            raise DingTalkReadGateway._failure(dataset, "record response")
        records_value = response.get("records")
        value_value = response.get("value")
        list_values = [
            value for value in (records_value, value_value) if isinstance(value, list)
        ]
        if len(list_values) != 1:
            raise DingTalkReadGateway._failure(dataset, "record response")
        return list_values[0]

    @staticmethod
    def _failure(dataset, failure_kind):
        return DingTalkReadError(f"{dataset}: {failure_kind}")
