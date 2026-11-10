"""BI people 查询域；SQL 与返回契约自原 queries.py 等价迁移。"""

from common.bi_web.query_domains.common import *


_PEOPLE_REGIONS = ("杭州", "绍兴")


_PEOPLE_CACHE_TTL_SECONDS = 60.0


_PEOPLE_CACHE = {}


_PEOPLE_CACHE_LOCK = threading.Lock()


def _people_params(params):
    """run 层共用解析：region（None=杭州+绍兴两榜合并）、month（缺省当前月）。"""
    month = params.get("month") or datetime.now().strftime("%Y-%m")
    return params.get("region"), month


def _people_as_of(month):
    """月参数 → mart_collect 锚点日：当前月=今天，历史月=月末。

    elapsed 以锚点截断（include_today=True 含当天），历史月取月末即
    全月工作日；未来月不在 month_options（数据月 ∪ 当前月），不设护栏。
    """
    first_day, last_day = month_bounds(month)
    today = datetime.now().date()
    if (first_day.year, first_day.month) == (today.year, today.month):
        return today
    return last_day


def _people_for_region(connection, region, month):
    """单区域人员行；dim_calendar 缺行（MartTaskError）降级为空列表。"""
    try:
        data = mart_collect(
            connection,
            region=region,
            business_date=_people_as_of(month),
            include_today=True,
        )
    except MartTaskError:
        return []
    return list(data.people)


def _people_snapshot(connection, region, month, scope=None):
    """l2-people 四卡共享的人员快照（模块级 60s 缓存，带锁）。

    页面一次加载的四张卡（三 scalar + 一 table）只触发一轮采集查询；
    region=None 时杭州+绍兴两榜合并后按 ``(-rate(None→-1), -completed,
    -target)`` 重排（排序键与 mart_collect 逐字相同），单区域沿用
    mart_collect 自带排序。

    锁覆盖检查与采集全程（与 app._TTLGate 同款语义）：同键并发首查
    只发起一轮采集，后到者等锁后直接命中缓存。

    行级 scope（M3）：mart_collect 的 region 校验在共享采集层、无法在
    本卡下推 SQL，故此处走 fail-closed **门**——钉死区域不在 scope 内
    即空榜；region=None 时只并入 scope 内的榜位（交集为空即空榜，绝不
    用「全域榜」冒充可见）。scope 指纹并入缓存键，避免不同可见范围
    互相命中同一份榜。
    """
    key = (region, month, scope_fingerprint(scope))
    with _PEOPLE_CACHE_LOCK:
        cached = _PEOPLE_CACHE.get(key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        if region is None:
            if scope is None:
                wanted = _PEOPLE_REGIONS
            else:
                wanted = tuple(
                    one for one in _PEOPLE_REGIONS if one in scope
                )
            people = []
            for one_region in wanted:
                people.extend(
                    _people_for_region(connection, one_region, month)
                )
            people.sort(
                key=lambda person: (
                    -(person["rate"] if person["rate"] is not None else -1),
                    -person["completed"],
                    -person["target"],
                )
            )
        elif scope is not None and region not in scope:
            people = []
        else:
            people = _people_for_region(connection, region, month)
        _PEOPLE_CACHE[key] = (
            time.monotonic() + _PEOPLE_CACHE_TTL_SECONDS,
            people,
        )
        return people


def run_kpi_people_count(connection, params) -> dict:
    """参与人数: 榜上人员数（unit=人，前端不做万换算）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month, scope_from_params(params))
    return {"chart": "scalar", "value": float(len(people)), "unit": "人"}


def run_kpi_people_completed(connection, params) -> dict:
    """Σ完成额: 已过工作日 sales_amount 合计（元）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month, scope_from_params(params))
    return {
        "chart": "scalar",
        "value": float(sum(person["completed"] for person in people)),
        "unit": _UNIT,
    }


def run_kpi_people_rate(connection, params) -> dict:
    """总达成率: Σcompleted÷Σtarget（非个人率平均；Σtarget=0 → None）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month, scope_from_params(params))
    completed = sum(person["completed"] for person in people)
    target = sum(person["target"] for person in people)
    rate = None if target == 0 else completed / target
    return {
        "chart": "scalar",
        "value": float(completed),
        "target": float(target),
        "rate": rate,
        "unit": _UNIT,
    }


def run_table_people_leaderboard(connection, params) -> dict:
    """人员榜: 服务端名次；未完成缺口=未填工作日数（spec §3.4）。"""
    region, month = _people_params(params)
    people = _people_snapshot(connection, region, month, scope_from_params(params))
    rows = [
        {
            "rank": index,
            "name": person["name"],
            "dept": person["dept"],
            "completed": float(person["completed"]),
            "target": float(person["target"]),
            "rate": person["rate"],
            "unfilled": person["unfilled"],
        }
        for index, person in enumerate(people, start=1)
    ]
    return {
        "chart": "table",
        "columns": [
            {"key": "rank", "title": "排名"},
            {"key": "name", "title": "姓名"},
            {"key": "dept", "title": "部门"},
            {"key": "completed", "title": "完成额", "format": "wan"},
            {"key": "target", "title": "月目标", "format": "wan"},
            {"key": "rate", "title": "达成率", "format": "percent"},
            {"key": "unfilled", "title": "未完成缺口"},
        ],
        "rows": rows,
    }

__all__ = ('_PEOPLE_REGIONS', '_PEOPLE_CACHE_TTL_SECONDS', '_PEOPLE_CACHE', '_PEOPLE_CACHE_LOCK', '_people_params', '_people_as_of', '_people_for_region', '_people_snapshot', 'run_kpi_people_count', 'run_kpi_people_completed', 'run_kpi_people_rate', 'run_table_people_leaderboard')
