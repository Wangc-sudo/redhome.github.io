# -*- coding: utf-8 -*-
"""共享业务口径（spec §9）：robot 与 pages-leaderboard 共同 import。"""
from .daily_report import (
    PersonSummary,
    achievement_rate,
    elapsed_workdays,
    fetch_filled_names,
    fetch_member,
    fetch_month_facts,
    fetch_region_members,
    fetch_unfilled_members,
    fetch_workdays,
    summarize_people,
    unfilled_members,
)

__all__ = [
    "PersonSummary",
    "achievement_rate",
    "elapsed_workdays",
    "fetch_filled_names",
    "fetch_member",
    "fetch_month_facts",
    "fetch_region_members",
    "fetch_unfilled_members",
    "fetch_workdays",
    "summarize_people",
    "unfilled_members",
]
