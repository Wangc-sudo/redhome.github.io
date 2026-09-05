# -*- coding: utf-8 -*-
"""日报机器人公共核心（占位模块，将被后续任务覆盖）。"""


def today_info(calendar, now=None):
    raise NotImplementedError


def fetch_status(config):
    raise NotImplementedError


def send_group(config, title, text, at_ids=None):
    raise NotImplementedError


def do_remind(config, state, now, day):
    raise NotImplementedError


def do_check(config, state, now, day):
    raise NotImplementedError


def org_sync(config, inputs, active_region):
    raise NotImplementedError


def recalc_totals(config):
    raise NotImplementedError


def check_data(config):
    raise NotImplementedError
