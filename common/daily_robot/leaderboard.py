# -*- coding: utf-8 -*-
"""榜单生成（占位模块，将被后续任务覆盖）。"""


def collect(config, include_today=False):
    raise NotImplementedError


def build_bc_markdown(config, url=None):
    raise NotImplementedError


def build_html(config, now, elapsed, people):
    raise NotImplementedError
