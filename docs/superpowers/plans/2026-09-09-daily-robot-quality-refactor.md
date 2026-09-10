# Daily Report Robot Quality Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Consolidate the Hangzhou and Shaoxing daily-report robots on one tested core while preserving regional configuration, adding Shaoxing delegated reporting, and making a controlled production migration possible.

**Architecture:** `common/daily_robot` remains the only business-logic layer. Regional directories retain configuration templates and explicit executable wrappers; the Stream listener is started once by a dual-region router and each child handler verifies its own group ID. Calendar, numeric parsing, reporting delegates, organization membership, leaderboard snapshots, and publication all use shared contracts.

**Tech Stack:** Python 3.12+, `dingtalk-stream`, DingTalk Notable API client, `unittest`/`pytest`, existing `common.dingtalk` and `common.test_group` helpers.

**Safety constraints:** Do not commit `config.json`, downloaded ECS source, credentials, user IDs, or environment tokens. Do not send group messages, DINGs, write a production table, modify cron/systemd, or switch the live listener during implementation. No Git commit is included because the user has not requested one.

---

## File structure

| Path | Responsibility |
|---|---|
| `common/calendar_utils.py` | Actual-month workday calculation and configured-month validation. |
| `common/daily_robot/numbers.py` | One finite-number parser used by reports, totals, validation, and leaderboards. |
| `common/daily_robot/core.py` | Reminder/DING recipient resolution, organization-to-table membership reconciliation, totals, and data checks. |
| `common/daily_robot/listener.py` | Conversation filtering, sender authorization, self-reporting, and configured delegated reporting. |
| `common/daily_robot/leaderboard.py` | Collection, broadcast Markdown, HTML, and dashboard snapshot construction. |
| `common/daily_robot/__init__.py` | Public exports for the new helpers. |
| `ops/report_robot_listener/dual_report_listener.py` | The only process that opens a DingTalk Stream connection for Hangzhou and Shaoxing. |
| `数字化/钉钉/杭州日报机器人/*.py` | Thin Hangzhou operational wrappers; no business logic. |
| `数字化/钉钉/shaoxing_daily_robot/*.py` | Thin Shaoxing operational wrappers; no business logic. |
| `数字化/钉钉/*日报机器人/config.example.json` | Region configuration templates, including delegated reporting only for Shaoxing. |
| `数字化/钉钉/*日报机器人/push_leaderboard.py` | Explicit regional wrappers around the shared snapshot builder. |
| `tests/common/test_daily_robot.py` | Core, calendar, number, recipients, organization-sync, totals, and data-check regression tests. |
| `tests/common/test_daily_robot_listener.py` | Self-report/delegated-report authorization and group-isolation tests. |
| `tests/common/test_daily_robot_leaderboard.py` | Day-31, invalid-number, first-day, Markdown/HTML, and snapshot tests. |
| `tests/common/test_dual_report_listener.py` | One-connection router dispatch tests. |

---

### Task 1: Make calendar and numeric handling shared and month-safe

**Files:**
- Modify: `common/calendar_utils.py`
- Create: `common/daily_robot/numbers.py`
- Modify: `common/daily_robot/core.py`
- Modify: `common/daily_robot/leaderboard.py`
- Modify: `common/daily_robot/__init__.py`
- Modify: `tests/common/test_daily_robot.py`
- Create: `tests/common/test_daily_robot_leaderboard.py`

- [ ] **Step 1: Add failing day-31 and non-finite-number tests.**

```python
from common.calendar_utils import Calendar
from common.daily_robot.numbers import parse_number


def test_calendar_includes_day_31_when_not_rest_day():
    calendar = Calendar(month=10, rest_days=[])
    assert calendar.workdays(year=2026)[-1] == 31
    assert calendar.total_for_year(2026) == 31


def test_calendar_excludes_configured_day_31_rest_day():
    calendar = Calendar(month=10, rest_days=[31])
    assert 31 not in calendar.workdays(year=2026)


def test_parse_number_accepts_signed_finite_amounts_and_rejects_non_finite_values():
    assert parse_number("1,234.5") == 1234.5
    assert parse_number("-80") == -80.0
    assert parse_number("NaN") is None
    assert parse_number("Infinity") is None
    assert parse_number("not-a-number") is None
```

- [ ] **Step 2: Run the focused tests and confirm the day-31 assertion fails against the old 1..30 implementation.**

Run: `pytest tests/common/test_daily_robot.py tests/common/test_daily_robot_leaderboard.py -q`

Expected before implementation: failure because `Calendar.workdays()` ends at day 30 and `numbers.py` is not importable.

- [ ] **Step 3: Replace the fixed 30-day calendar with actual month length.**

Replace the cached `self._workdays` design in `common/calendar_utils.py` with a dynamic calculation. The public behavior remains the same for callers that omit `year`.

```python
import calendar as stdlib_calendar
from datetime import datetime


class Calendar:
    def __init__(self, month, rest_days):
        self.month = int(month)
        self.rest_days = {int(day) for day in rest_days}

    def _year(self, year):
        return int(year or datetime.now().year)

    def workdays(self, year=None):
        max_day = stdlib_calendar.monthrange(self._year(year), self.month)[1]
        return [day for day in range(1, max_day + 1) if day not in self.rest_days]

    def total_for_year(self, year=None):
        return len(self.workdays(year))

    @property
    def total(self):
        return self.total_for_year()

    def elapsed(self, include_today=False, now=None):
        now = now or datetime.now()
        return [
            day for day in self.workdays(now.year)
            if day < now.day or (include_today and day == now.day)
        ]
```

Keep `check_month`, `is_rest`, and `today_state`; change `today_state` to use the unchanged `is_rest(now.day)` behavior.

- [ ] **Step 4: Add the finite shared number parser.**

Create `common/daily_robot/numbers.py`:

```python
import math


def parse_number(value):
    if value is None:
        return None
    try:
        parsed = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
```

Signed finite amounts are intentionally supported because the existing report grammar accepts negative adjustments. Empty and non-finite values are invalid and must never enter totals, rankings, or completion rates.

- [ ] **Step 5: Make all report calculations consume the shared helpers.**

In `core.py`:

```python
from common.calendar_utils import Calendar
from common.daily_robot.numbers import parse_number


def _parse_num(value):
    return parse_number(value)


def _workdays(calendar_config, now=None):
    now = now or datetime.now()
    return Calendar.from_config(calendar_config).workdays(now.year)
```

Use `_workdays(calendar)` in both `recalc_totals` and `check_data`; remove both `range(1, 31)` expressions. In `check_data`, replace direct `float(...)` conversion with `_parse_num` and report invalid values when the parser returns `None`.

In `leaderboard.py`, replace its local `_parse_num` implementation with `parse_number`, and derive `workdays` from `Calendar.from_config(calendar).workdays(now.year)` in `collect`, `build_bc_markdown`, and `build_html`.

Export `parse_number` from `common/daily_robot/__init__.py`.

- [ ] **Step 6: Re-run focused tests and the whole common suite.**

Run:

```bash
pytest tests/common/test_daily_robot.py tests/common/test_daily_robot_leaderboard.py -q
pytest tests/common -q
```

Expected: all tests pass; no module still uses `range(1, 31)` for a daily-report calendar.

---

### Task 2: Make Shaoxing delegated reporting and reminder recipients configuration-driven

**Files:**
- Modify: `common/daily_robot/core.py`
- Modify: `数字化/钉钉/shaoxing_daily_robot/config.example.json`
- Modify: `数字化/钉钉/杭州日报机器人/config.example.json`
- Modify: `tests/common/test_daily_robot.py`

- [ ] **Step 1: Add failing recipient-resolution tests.**

Add this `unittest.TestCase` to `tests/common/test_daily_robot.py` so the focused selector in Step 2 exists:

```python
from common.daily_robot.core import resolve_report_recipients


class TestReportRecipients(unittest.TestCase):
    def test_routes_store_to_proxy_without_personal_ding(self):
        config = {
            "members": {"Alice": "u-alice"},
            "proxies": {
                "诸暨门店": {"proxy": "沈聪", "uid": "u-shen", "keyword": "门店"}
            },
        }

        recipients = resolve_report_recipients(
            config, ["Alice", "诸暨门店", "Unknown"]
        )

        self.assertEqual(recipients.direct_at_ids, ["u-alice"])
        self.assertEqual(recipients.proxy_at_ids, ["u-shen"])
        self.assertEqual(recipients.personal_ding_ids, ["u-alice"])
        self.assertEqual(
            recipients.proxy_notes,
            ["诸暨门店（由 沈聪 代报：@提醒事项 门店 数字）"],
        )
        self.assertEqual(recipients.missing_names, ["Unknown"])

    def test_proxy_rule_wins_over_accidental_member_mapping(self):
        config = {
            "members": {"诸暨门店": "u-store"},
            "proxies": {
                "诸暨门店": {"proxy": "沈聪", "uid": "u-shen", "keyword": "门店"}
            },
        }

        recipients = resolve_report_recipients(config, ["诸暨门店"])

        self.assertEqual(recipients.direct_at_ids, [])
        self.assertEqual(recipients.proxy_at_ids, ["u-shen"])
        self.assertEqual(recipients.personal_ding_ids, [])
```

- [ ] **Step 2: Run the focused test and confirm the resolver does not exist.**

Run: `pytest tests/common/test_daily_robot.py::TestReportRecipients -q`

Expected before implementation: import failure for `resolve_report_recipients`.

- [ ] **Step 3: Implement recipient resolution without exposing proxy targets to personal DING.**

Add the following type and function to `core.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class ReportRecipients:
    direct_at_ids: list[str]
    proxy_at_ids: list[str]
    personal_ding_ids: list[str]
    proxy_notes: list[str]
    missing_names: list[str]


def resolve_report_recipients(config, unfilled_names):
    members = config.get("members", {})
    proxies = config.get("proxies", {})
    direct_at_ids, proxy_at_ids, personal_ding_ids = [], [], []
    proxy_notes, missing_names = [], []

    for name in unfilled_names:
        rule = proxies.get(name)
        if rule and rule.get("uid") and rule.get("proxy") and rule.get("keyword"):
            proxy_at_ids.append(rule["uid"])
            proxy_notes.append(
                f"{name}（由 {rule['proxy']} 代报：@提醒事项 {rule['keyword']} 数字）"
            )
            continue

        member_uid = members.get(name)
        if member_uid:
            direct_at_ids.append(member_uid)
            personal_ding_ids.append(member_uid)
        else:
            missing_names.append(name)

    return ReportRecipients(
        direct_at_ids=list(dict.fromkeys(direct_at_ids)),
        proxy_at_ids=list(dict.fromkeys(proxy_at_ids)),
        personal_ding_ids=list(dict.fromkeys(personal_ding_ids)),
        proxy_notes=proxy_notes,
        missing_names=missing_names,
    )
```

- [ ] **Step 4: Apply the resolver in reminder and 20:00 check flows.**

In `do_remind`, build group mentions from both direct and proxy IDs and append the proxy guidance after the unfilled-name list:

```python
recipients = resolve_report_recipients(config, unfilled)
at_ids = list(dict.fromkeys(recipients.direct_at_ids + recipients.proxy_at_ids))
if recipients.proxy_notes:
    lines.extend(["", "代报说明：", *[f"- {note}" for note in recipients.proxy_notes]])
if recipients.missing_names:
    lines.append(f"（{'、'.join(recipients.missing_names)} 未在通讯录映射中，无法@，请手动提醒）")
```

In `do_check`, resolve recipients once, then use this exact recipient split:

```python
recipients = resolve_report_recipients(config, unfilled)
cc_ids = [
    user_id
    for name, user_id in config.get("ccUsers", {}).items()
    if not name.startswith("_") and user_id
]
at_ids = list(dict.fromkeys(
    recipients.direct_at_ids + recipients.proxy_at_ids + cc_ids
))
ding_ids = recipients.personal_ding_ids
```

Pass `at_ids` to `send_group` and use `ding_ids` for `dws ding message send --users`. A delegated store row is therefore mentioned in the group through its proxy but never receives an individual DING. Reuse `recipients.missing_names` and `recipients.proxy_notes` in the existing message construction; do not re-derive names from `members`.

- [ ] **Step 5: Document only the Shaoxing delegated rule in templates.**

Add this key to the Shaoxing template after `members`:

```json
"proxies": {
  "诸暨门店": {
    "proxy": "沈聪",
    "uid": "<沈聪userId>",
    "keyword": "门店"
  }
},
```

Add an empty object to the Hangzhou template:

```json
"proxies": {},
```

Do not add real user IDs to either template.

- [ ] **Step 6: Run recipient and reminder regression tests.**

Run:

```bash
pytest tests/common/test_daily_robot.py -q
```

Expected: direct reporters are mentioned and DINGed; the delegated store target is mentioned through its proxy and excluded from individual DING.

---

### Task 3: Add secure delegated report handling to the shared Stream handler

**Files:**
- Modify: `common/daily_robot/listener.py`
- Create: `tests/common/test_daily_robot_listener.py`
- Modify: `common/daily_robot/__init__.py`

- [ ] **Step 1: Write failing handler tests for group isolation and delegated reports.**

Use mocked incoming messages and a mocked `DingTalkClient`. The test must prove that a matching proxy UID can write only its configured target and that another group cannot trigger the handler.

```python
async def test_proxy_can_report_only_its_configured_target(handler, incoming):
    incoming.conversation_id = "shaoxing-group"
    incoming.sender_staff_id = "u-shen"
    incoming.text.content = "门店 1,280"

    await handler.process(make_callback(incoming))

    handler._write_and_reply.assert_called_once()
    assert handler._write_and_reply.call_args.kwargs["target_name"] == "诸暨门店"
    assert handler._write_and_reply.call_args.kwargs["reported_by"] == "沈聪"


async def test_other_group_is_ignored_before_sender_authorization(handler, incoming):
    incoming.conversation_id = "other-group"
    incoming.sender_staff_id = "u-shen"
    incoming.text.content = "门店 1280"

    await handler.process(make_callback(incoming))

    handler._write_and_reply.assert_not_called()
    handler.reply_text.assert_not_called()
```

- [ ] **Step 2: Run the listener test and confirm current handler cannot resolve a proxy.**

Run: `pytest tests/common/test_daily_robot_listener.py -q`

Expected before implementation: the handler treats the proxy as an unauthorized sender or reports under the proxy’s own name.

- [ ] **Step 3: Refresh all runtime identity fields from the local config file.**

Replace `_refresh_uid2name` with `_refresh_identities`. Initialize runtime state in `__init__` so the group filter works before the first successful file reload:

```python
self.config = dict(config)
self.runtime_config = dict(config)
self.base_dir = Path(config.get("baseDir", "."))
self._member_by_uid = {}
self._proxy_rules_by_uid = {}
self._identities_loaded = False
self._identities_ts = 0.0
self._identities_ttl = 300
```

On each due reload, read `config.json`, restore the local path metadata, build all values in local variables, then atomically replace the runtime configuration and maps only after success:

```python
runtime_config = json.loads((self.base_dir / "config.json").read_text(encoding="utf-8"))
runtime_config["baseDir"] = str(self.base_dir)
member_by_uid = {
    user_id: name
    for name, user_id in runtime_config.get("members", {}).items()
    if not name.startswith("_") and user_id
}
proxy_rules_by_uid = {}
for target, rule in runtime_config.get("proxies", {}).items():
    if rule.get("uid") and rule.get("proxy") and rule.get("keyword"):
        proxy_rules_by_uid.setdefault(rule["uid"], []).append(
            {"target": target, **rule}
        )

self.config = runtime_config
self.runtime_config = runtime_config
self._member_by_uid = member_by_uid
self._proxy_rules_by_uid = proxy_rules_by_uid
self._identities_loaded = True
self._identities_ts = now
```

Use `_identities_loaded` rather than an empty-map test to prevent an empty but valid configuration from being reloaded for every callback. On parsing failure, retain the previous complete runtime configuration and identity maps. A proxy may own multiple configured targets; all rules must be retained and resolved by their distinct keyword in Step 5.

- [ ] **Step 4: Filter the conversation before parsing values.**

At the beginning of `process`, after creating `incoming`, enforce the configured group ID:

```python
expected_conversation_id = self.runtime_config["robot"].get("openConversationId")
if expected_conversation_id and incoming.conversation_id != expected_conversation_id:
    self.log(f"[过滤]非本群消息 conv={incoming.conversation_id or '?'}")
    return AckMessage.STATUS_OK, "OK"
```

This is defense in depth; the dual router in Task 6 performs the first routing decision.

- [ ] **Step 5: Parse the two authorized grammars explicitly.**

Keep self-report input compatible with the existing `@提醒事项 数字` syntax, but make the accepted amount the complete message body after the mention. For a proxy, require its configured keyword immediately before the amount:

```python
def _parse_self_amount(text):
    normalized = text.replace("，", ",").strip()
    match = re.fullmatch(
        r"(?:@?提醒事项\s+)?(-?\d[\d,]*(?:\.\d+)?)", normalized
    )
    return parse_number(match.group(1)) if match else None


def _parse_proxy_amount(text, keyword):
    pattern = rf"(?:^|\s){re.escape(keyword)}\s+(-?\d[\d,]*(?:\.\d+)?)\s*$"
    match = re.search(pattern, text.replace("，", ","))
    return parse_number(match.group(1)) if match else None
```

After the group filter and identity refresh, resolve the sender in this order:

```python
proxy_rules = self._proxy_rules_by_uid.get(sender_uid, [])
proxy_matches = [
    (rule, value)
    for rule in proxy_rules
    if (value := _parse_proxy_amount(text, rule["keyword"])) is not None
]
if len(proxy_matches) == 1:
    rule, value = proxy_matches[0]
    self._write_and_reply(
        incoming,
        target_name=rule["target"],
        reported_by=rule["proxy"],
        value=value,
        col=col,
        day=day,
        is_proxy=True,
    )
    return
if proxy_rules and _parse_self_amount(text) is None:
    self._reply(incoming, "代报格式：@提醒事项 门店 数字，例如：@提醒事项 门店 12800")
    return

sender_name = self._member_by_uid.get(sender_uid)
if not sender_name:
    self.log(f"门禁拦截: 非责任人(uid={sender_uid}) 尝试使用: {text[:30]}")
    self._reply(
        incoming,
        "⛔ 报数功能仅限销售日报责任人使用。\n"
        "如需填写日报请联系管理员，或在表格中直接填写。",
    )
    return
value = _parse_self_amount(text)
if value is None:
    self._reply(
        incoming,
        f"{sender_name} 你好～报数格式：@提醒事项 数字\n"
        "例如：@提醒事项 12800（当天无销量报 0）",
    )
    return
self._write_and_reply(
    incoming,
    target_name=sender_name,
    reported_by=sender_name,
    value=value,
    col=col,
    day=day,
    is_proxy=False,
)
```

A proxy can therefore self-report with only an amount, but any non-self-format text that does not exactly match one configured proxy keyword receives the delegated-report format prompt and cannot write a table record. Add a test in `tests/common/test_daily_robot_listener.py` for a proxy with two target rules to prove each keyword writes only its matching target.

- [ ] **Step 6: Centralize record writes and acknowledgement messages.**

Create `_write_and_reply(incoming, *, target_name, reported_by, value, col, day, is_proxy)` that performs the existing read-old-value, update, total calculation, logging, and response. For a delegated entry, include a single explicit acknowledgement line:

```python
prefix = f"{reported_by} 已代报 {target_name}" if is_proxy else "已记录"
lines = [f"✅ {prefix} {datetime.now().month}月{day}日（周{weekday}）销量：{value}"]
```

Resolve the record ID with `target_name`, never the proxy name. Keep the existing overwrite notice. Remove the fixed `"9月销量目标（万）"` fallback in `_calc_progress`; look up only `f"{calendar_month}月销量目标（万）"`.

- [ ] **Step 7: Run listener and existing common tests.**

Run:

```bash
pytest tests/common/test_daily_robot_listener.py tests/common/test_daily_robot.py -q
```

Expected: self-report behavior remains unchanged, proxy reports are target-bound, unauthorized users are rejected, and messages from other groups have no side effects.

---

### Task 4: Reconcile organization archives with actual table responsibility rows

**Files:**
- Modify: `common/daily_robot/core.py`
- Modify: `数字化/钉钉/杭州日报机器人/org_sync.py`
- Modify: `数字化/钉钉/shaoxing_daily_robot/org_sync.py`
- Modify: `tests/common/test_daily_robot.py`

- [ ] **Step 1: Replace the current new-member expectation with table-authority tests.**

Add these methods to the existing `TestOrgSync` class, using its `_config` and `_write_snapshot` helpers:

```python
def test_archives_new_joiner_without_whitelisting_it_until_a_table_row_exists(self):
    with tempfile.TemporaryDirectory() as directory:
        snapshot = self._write_snapshot(
            directory, [{"userInfo": {"name": "Alice", "userId": "u1"}}]
        )
        config = self._config(directory)

        changed, changes = org_sync(
            config, {"shaoxing": snapshot}, "shaoxing", table_names=set()
        )

        self.assertTrue(changed)
        self.assertEqual(changes, ["[shaoxing] 入职/新增: Alice (u1)"])
        self.assertEqual(config["org"]["archives"]["shaoxing"], {"Alice": "u1"})
        self.assertEqual(config["members"], {})


def test_reconciles_members_when_archive_is_unchanged_but_table_row_is_added(self):
    with tempfile.TemporaryDirectory() as directory:
        snapshot = self._write_snapshot(
            directory, [{"userInfo": {"name": "Alice", "userId": "u1"}}]
        )
        config = self._config(directory)
        config["org"]["archives"]["shaoxing"] = {"Alice": "u1"}

        changed, changes = org_sync(
            config, {"shaoxing": snapshot}, "shaoxing", table_names={"Alice"}
        )

        self.assertTrue(changed)
        self.assertEqual(changes, [])
        self.assertEqual(config["members"], {"Alice": "u1"})
```

Update existing add/remove/ID-change tests to pass `table_names={"Alice"}` where the table row is meant to exist, and change the old `test_no_changes_when_snapshot_matches_archive` expectation to `changed is False` only when both the archive and `members` already match `table_names`.

- [ ] **Step 2: Run the focused organization tests and confirm old behavior adds every organizational joiner.**

Run: `pytest tests/common/test_daily_robot.py::TestOrgSync -q`

Expected before implementation: the first test fails because `members` receives Alice without a table row.

- [ ] **Step 3: Add a table-owner fetch helper.**

Add to `core.py`:

```python
def fetch_table_names(config):
    base = config["base"]
    client = DingTalkClient.from_config(config["dingtalk"])
    records = client.list_records(base["baseId"], base["tableId"])
    return {
        str((record.get("fields") or {}).get("责任人")).strip()
        for record in records
        if (record.get("fields") or {}).get("责任人")
        and "合计" not in str((record.get("fields") or {}).get("责任人"))
    }
```

- [ ] **Step 4: Rebuild `members` from the active archive and table names.**

Change the signature to:

```python
def org_sync(config, inputs, active_region, table_names):
```

Remove the current early `if not changes: return False, []` block. After all successfully loaded archives have been updated, calculate the active whitelist from the active archive on every call where the active-region snapshot exists:

```python
active_members = {
    _apply_alias(real_name, aliases): user_id
    for real_name, user_id in archives.get(active_region, {}).items()
    if _apply_alias(real_name, aliases) in table_names
}
members_changed = config.get("members", {}) != active_members
config["members"] = active_members
org["lastSync"] = datetime.now().isoformat(timespec="seconds")
config["org"] = org
return bool(changes) or members_changed, changes
```

If the active-region snapshot is absent, retain the existing `members` mapping and return only archive changes from other valid snapshots; do not clear a whitelist based on incomplete input. Keep archives for non-active regions intact. A newly hired employee is archived immediately, but cannot be mentioned, DINGed, or use report entry until a table row exists. A departed employee is removed from the active archive and whitelist even if their historical row remains in the table.

- [ ] **Step 5: Use the configured active-region name in both wrappers.**

In each regional `org_sync.py`, import `fetch_table_names`, load the config, then call:

```python
active_region = config["region"]["activeOrgRegion"]
table_names = fetch_table_names(config)
changed, changes = org_sync(config, INPUTS, active_region, table_names)
if changed:
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
```

Do not hard-code `"hangzhou"` or `"shaoxing"` in the call. Send the existing personnel-change group message only when `changes` is non-empty; a `changed=True, changes=[]` result is a table-only whitelist reconciliation and must persist silently. In the Hangzhou wrapper, compute `hz_changes` before the notification branch and retain the existing region filter; in the Shaoxing wrapper, guard the whole archive-change log and message block with `if changes:`.

- [ ] **Step 6: Run organization and wrapper import tests.**

Run:

```bash
pytest tests/common/test_daily_robot.py::TestOrgSync -q
python -m compileall common/daily_robot/core.py "数字化/钉钉/杭州日报机器人/org_sync.py" "数字化/钉钉/shaoxing_daily_robot/org_sync.py"
```

Expected: archive changes are recorded regardless of table state, while `members` is exactly the active archived people with non-total table rows.

---

### Task 5: Unify leaderboard collection and dashboard publication contracts

**Files:**
- Modify: `common/daily_robot/leaderboard.py`
- Modify: `common/daily_robot/__init__.py`
- Modify: `数字化/钉钉/杭州日报机器人/push_leaderboard.py`
- Create: `数字化/钉钉/shaoxing_daily_robot/push_leaderboard.py`
- Modify: `tests/common/test_daily_robot_leaderboard.py`

- [ ] **Step 1: Write failing day-31 and snapshot-contract tests.**

Create `tests/common/test_daily_robot_leaderboard.py` using the existing `unittest` style:

```python
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch

from common.daily_robot.leaderboard import build_snapshot


class TestBuildSnapshot(unittest.TestCase):
    def _config(self):
        return {
            "calendar": {"month": 10, "restDays": []},
            "region": {
                "name": "shaoxing",
                "displayName": "绍兴",
                "deptOrder": ["绍兴"],
                "deptLabel": {},
                "broadcastExclude": [],
            },
            "dingtalk": {"appKey": "key", "appSecret": "secret"},
            "base": {"baseId": "base", "tableId": "table"},
        }

    @patch("common.daily_robot.leaderboard.DingTalkClient.from_config")
    def test_uses_region_labels_and_day_31(self, from_config):
        client = MagicMock()
        client.list_records.return_value = [{
            "id": "record-1",
            "fields": {
                "责任人": "Alice", "项目部": "绍兴", "10月销量目标（万）": "1000",
                "1日": "100",
            },
        }]
        from_config.return_value = client

        payload = build_snapshot(
            self._config(), include_today=False, now=datetime(2026, 10, 31, 8, 30)
        )

        self.assertEqual(payload["month"], 10)
        self.assertEqual(payload["progress"]["total"], 31)
        self.assertEqual(payload["stat_date"], "10月30日")
        self.assertEqual(payload["depts"][0]["label"], "绍兴")

    @patch("common.daily_robot.leaderboard.DingTalkClient.from_config")
    def test_first_workday_has_zero_progress_without_division_error(self, from_config):
        client = MagicMock()
        client.list_records.return_value = []
        from_config.return_value = client

        payload = build_snapshot(self._config(), now=datetime(2026, 10, 1, 8, 30))

        self.assertEqual(payload["progress"], {"elapsed": 0, "total": 31, "pct": 0})
        self.assertEqual(payload["stat_date"], "—")
```

- [ ] **Step 2: Run the focused tests and confirm the current publisher cannot import its old leaderboard constants.**

Run: `pytest tests/common/test_daily_robot_leaderboard.py -q`

Expected before implementation: no `build_snapshot` function; `push_leaderboard.py` still imports symbols that thin `leaderboard_report.py` does not export.

- [ ] **Step 3: Give `collect` an injectable clock and use the shared calendar.**

Change its signature to:

```python
def collect(config, include_today=False, now=None):
    now = now or datetime.now()
    calendar = Calendar.from_config(config["calendar"])
    calendar.check_month(now)
    workdays = calendar.workdays(now.year)
    elapsed = [
        day for day in workdays
        if day < now.day or (include_today and day == now.day)
    ]
```

Invalid or non-finite daily values remain counted as unfilled and do not contribute to `completed`.

- [ ] **Step 4: Add one shared JSON snapshot builder.**

Add `build_snapshot(config, include_today=False, now=None)` to `leaderboard.py`. It calls `collect(config, include_today=include_today, now=now)`, aggregates departments after applying `region["broadcastExclude"]`, and returns this exact contract:

```python
now, elapsed, people = collect(config, include_today=include_today, now=now)
region = config["region"]
calendar = Calendar.from_config(config["calendar"])
total_workdays = calendar.total_for_year(now.year)
progress = len(elapsed) / total_workdays if total_workdays else 0
stat_through = f"{now.month}月{elapsed[-1]}日" if elapsed else "—"
total_completed = sum(person["completed"] for person in people)
total_target = sum(person["target"] for person in people)
overall_rate = total_completed / total_target if total_target else 0
dept_label = region.get("deptLabel", {})
broadcast_people = [
    person for person in people
    if person["name"] not in region.get("broadcastExclude", [])
]
dept_groups = {}
for person in broadcast_people:
    dept_groups.setdefault(person["dept"], []).append(person)
ordered_departments = []
for dept_name in region.get("deptOrder", []) + [
    name for name in dept_groups if name not in region.get("deptOrder", [])
]:
    members = dept_groups.get(dept_name, [])
    if not members:
        continue
    completed = sum(member["completed"] for member in members)
    target = sum(member["target"] for member in members)
    ordered_departments.append((dept_name, members, completed, target))
ordered_departments.sort(key=lambda item: -(item[2] / item[3] if item[3] else 0))

people_payload = [
    {
        "name": person["name"],
        "dept": person["dept"],
        "dept_label": dept_label.get(person["dept"], person["dept"]),
        "target": person["target"],
        "completed": person["completed"],
        "rate": person["rate"],
        "unfilled": person["unfilled"],
    }
    for person in people
]

department_payload = [
    {
        "name": dept_name,
        "label": dept_label.get(dept_name, dept_name),
        "count": len(members),
        "completed": sum(member["completed"] for member in members),
        "target": sum(member["target"] for member in members),
        "rate": completed / target if target else 0,
        "unfilled_total": sum(member["unfilled"] for member in members),
    }
    for dept_name, members, completed, target in ordered_departments
]

{
    "year": now.year,
    "month": config["calendar"]["month"],
    "stat_date": stat_through,
    "generated_at": now.strftime("%Y-%m-%d %H:%M"),
    "stat_through": stat_through,
    "progress": {"elapsed": len(elapsed), "total": total_workdays, "pct": progress},
    "overall": {"completed": total_completed, "target": total_target, "rate": overall_rate},
    "people": people_payload,
    "depts": department_payload,
}
```

`total_completed` and `total_target` include every non-total record, matching the existing overall-card behavior. `department_payload` excludes `broadcastExclude`, matching the broadcast behavior, and must be sorted by descending `rate` after aggregation. Set `progress` to `0` if the workday total is zero. Set `stat_through` and `stat_date` to `"—"` when `elapsed` is empty. Export `build_snapshot` from `__init__.py`.

- [ ] **Step 5: Replace legacy imports in each publisher with the shared builder.**

In both regional `push_leaderboard.py` files, delete the `leaderboard_report` import and the duplicated payload aggregation. Retain the existing `push` function and replace payload construction with:

```python
from common.calendar_utils import CalendarError
from common.daily_robot import build_snapshot

CONFIG_FILE = BASE_DIR / "config.json"


def build_payload(config, include_today=False):
    return build_snapshot(config, include_today=include_today)


def main():
    include_today = "--include-today" in sys.argv
    if "--url" in sys.argv:
        global URL
        URL = sys.argv[sys.argv.index("--url") + 1]
    if not URL:
        print("ERROR: 未配置后端地址（--url 或环境变量 LEADERBOARD_URL）")
        sys.exit(2)
    if not TOKEN:
        print("ERROR: 未配置推送令牌（环境变量 LEADERBOARD_TOKEN）")
        sys.exit(2)

    config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    config["baseDir"] = str(BASE_DIR)
    try:
        payload = build_payload(config, include_today)
    except CalendarError as error:
        print(f"SKIP: {error}")
        sys.exit(3)

    try:
        response = push(URL, payload)
    except Exception as error:
        print(f"ERROR: 推送失败 {URL}: {error}")
        sys.exit(2)
    print(f"{'OK' if response.get('ok') else 'WARN'}: 已推送 {payload['stat_date']} 快照")
    sys.exit(0 if response.get("ok") else 2)
```

Retain the existing HTTP request and `LEADERBOARD_URL` / `LEADERBOARD_TOKEN` boundary. Do not import regional constants from `leaderboard_report.py`.

- [ ] **Step 6: Run leaderboard and publisher checks.**

Run:

```bash
pytest tests/common/test_daily_robot_leaderboard.py -q
python -m compileall common/daily_robot/leaderboard.py "数字化/钉钉/杭州日报机器人/push_leaderboard.py" "数字化/钉钉/shaoxing_daily_robot/push_leaderboard.py"
```

Expected: a first-day snapshot is valid, October includes day 31, and both publishers call the same data contract.

---

### Task 6: Enforce one Stream connection with explicit group routing

**Files:**
- Create: `ops/report_robot_listener/dual_report_listener.py`
- Modify: `数字化/钉钉/杭州日报机器人/hangzhou_listener.py`
- Modify: `数字化/钉钉/shaoxing_daily_robot/shaoxing_listener.py`
- Create: `tests/common/test_dual_report_listener.py`

- [ ] **Step 1: Write a failing router dispatch test.**

```python
async def test_dual_router_sends_callback_only_to_matching_group(router, callback):
    callback.data = make_message_data(conversation_id="shaoxing-group")

    await router.process(callback)

    router.handlers["shaoxing-group"].process.assert_awaited_once_with(callback)
    router.handlers["hangzhou-group"].process.assert_not_awaited()
```

- [ ] **Step 2: Create one composite callback handler.**

Create `ops/report_robot_listener/dual_report_listener.py` with a `DualReportRouter` that owns one `ReportHandler` per configured conversation ID:

```python
class DualReportRouter(dingtalk_stream.ChatbotHandler):
    def __init__(self, configs, log_fn=print):
        super().__init__()
        configs = list(configs)
        self.log = log_fn
        conversation_ids = [
            config.get("robot", {}).get("openConversationId")
            for config in configs
        ]
        if not all(conversation_ids):
            raise RuntimeError("日报群缺少 openConversationId")
        if len(set(conversation_ids)) != len(conversation_ids):
            raise RuntimeError("日报群 openConversationId 重复")
        self.handlers = {
            config["robot"]["openConversationId"]: ReportHandler(config, log_fn=log_fn)
            for config in configs
        }

    async def process(self, callback):
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
        except Exception:
            return AckMessage.STATUS_OK, "OK"
        handler = self.handlers.get(incoming.conversation_id)
        if handler is None:
            self.log(f"[过滤]未知日报群 conv={incoming.conversation_id or '?'}")
            return AckMessage.STATUS_OK, "OK"
        return await handler.process(callback)
```

Load both JSON paths from `--hangzhou-config` and `--shaoxing-config`. Set each `config["baseDir"]` to its config directory. Validate that both configurations have the same `appKey` and `appSecret`; raise a generic `RuntimeError("双群监听应用凭据不一致")` without printing either credential. Register only `DualReportRouter` for the chatbot topic and call `start_forever()` once.

- [ ] **Step 3: Prevent accidental second Stream connections from regional entry points.**

Replace each regional listener `main()` body with a fail-fast message:

```python
def main():
    raise SystemExit(
        "请通过 ops/report_robot_listener/dual_report_listener.py 启动杭州和绍兴监听服务"
    )
```

Keep these files as compatibility markers for operators; do not delete them during this release.

- [ ] **Step 4: Run router and listener tests.**

Run:

```bash
pytest tests/common/test_dual_report_listener.py tests/common/test_daily_robot_listener.py -q
python -m compileall ops/report_robot_listener/dual_report_listener.py
```

Expected: a callback is sent to one region handler only, and individual regional wrappers cannot open competing Stream connections.

---

### Task 7: Execute a no-side-effect regression pass and prepare the production migration

**Files:**
- Modify only if tests reveal an implementation defect in the files above.
- Do not modify ECS files, systemd, cron, or production configuration in this task.

- [ ] **Step 1: Run the complete local test suite.**

Run:

```bash
pytest -q
python -m compileall common ops "数字化/钉钉/杭州日报机器人" "数字化/钉钉/shaoxing_daily_robot"
git diff --check
```

Expected: tests pass, compilation succeeds, and the diff has no whitespace errors.

- [ ] **Step 2: Verify secrets remain excluded.**

Run:

```bash
git status --short
git check-ignore "数字化/钉钉/杭州日报机器人/config.json" "数字化/钉钉/shaoxing_daily_robot/config.json"
```

Expected: real configuration files are ignored and no downloaded `E:/tmp/server-code` content is staged or copied into the repository.

- [ ] **Step 3: Perform ECS read-only preflight before any deployment.**

Capture, without editing, the following from ECS: the current `/opt/report-robot-listener/dual_report_listener.py`, listener service definition and status, all relevant crontab entries, currently running Python listener commands, and the release paths for both region directories. Compare the router’s group-ID dispatch behavior with Task 6 before choosing the migration command.

Expected: one documented source of truth for the active listener command and all scheduled reminder, organization, total-recalculation, leaderboard, and publication tasks.

- [ ] **Step 4: Test only against the existing test-group routing configuration.**

Use copied test configuration with `common.test_group.resolve_target` selecting the test group. Run read-only `--status` commands first. Then exercise one self-report and one configured delegated-report scenario only against test table records and test group. Verify: target row, acknowledgement wording, no personal DING for the delegated store, and no cross-group response.

Expected: test table values and messages match the regression tests; no production group or production table is touched.

- [ ] **Step 5: Create a reversible release switch after explicit deployment approval.**

Publish the verified repository revision to a new versioned release directory on ECS, leave the prior release directory intact, point the listener service and scheduled commands at the new release, then restart only the single dual-router service. Confirm health through logs and a read-only status command. If any smoke check fails, restore the prior service target and restart it; do not edit or delete historical data.

Expected: exactly one Stream listener process is running and both groups route to their matching handler.

---

## Self-review

- **Coverage:** Tasks 1–5 address calendar, parsing, delegate, organization, totals/data-check, leaderboard, and publisher defects; Task 6 prevents random Stream delivery; Task 7 separates local/test verification from production migration.
- **Boundary check:** DingTalk writes remain inside the existing client calls. The new resolver and router are deterministic helpers with unit coverage. Regional business rules are configuration, not forked Python.
- **Compatibility:** Self-report input remains supported; signed finite figures remain supported; legacy regional listener files are retained but cannot accidentally create duplicate Stream clients.
- **Operational safety:** No plan step sends to a production group or changes ECS state until an explicit, separate deployment approval.
