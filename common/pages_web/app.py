# -*- coding: utf-8 -*-
"""pages-web：公开榜单页请求时渲染服务（2026-10-13 cron 解耦）。

背景：榜单页（qudao/shaoxing/hangzhou.html 等）原由 pages-* 定时管线
生成静态文件、nginx 纯静态分发——页面新鲜度被钉在 cron 上，调度停摆
页面就静默腐烂（2026-10-12 实测）。本服务改为请求时现场查库渲染，
TTL 缓存防惊群；cron 从页面链路退场（pages-* 管线停用）。

降级链（与 nginx @static 兜底配合）::

    请求 → 缓存/TTL 渲染 → 成功即双写 /output（静态兜底持续保温）
         → 渲染失败 → 磁盘 last-good（200）
         → 磁盘也无 → 503 → nginx @static 吃旧静态文件

公开页：无登录态（不读 BI_WEB_SESSION_SECRET），只读 mart + 写 /output。
路由兼容当前页 ``{region}.html`` 与存档页 ``{region}-YYYY-MM-DD.html``。
"""

import logging
import re
import sys
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

_LOGGER = logging.getLogger("pages-web")

#: 页面名安全闸：region 当前页或 ``region-YYYY-MM-DD`` 存档页（其余 404，
#: 拒绝路径穿越等一切非榜单形态）。
_PAGE_RE = re.compile(
    r"^(?P<region>[a-z0-9_]+)(?P<day>-\d{4}-\d{2}-\d{2})?\.html$"
)

#: 当前页缓存 TTL（秒）：榜单数据为日粒度，5 分钟足够新鲜且防 DB 抖动。
_PAGE_TTL_SECONDS = 300


def load_settings():
    from common.public_data.settings import Settings
    return Settings.from_environment()


def _mart_connector(settings):
    @contextmanager
    def connector():
        from common.public_data.db import connect
        connection = connect(settings.mart_database)
        try:
            yield connection
        finally:
            connection.close()

    return connector


def _default_renderer(conn, *, region, cfg, business_date, now, output):
    """渲染核心复用 mart_cli（lazy import：保持模块面轻，测试可整体注入）。"""
    from common.daily_robot.mart_cli import render_leaderboard_page
    return render_leaderboard_page(
        conn, region=region, cfg=cfg, business_date=business_date,
        now=now, output=output,
    )


def create_app(*, settings=None, db_connector=None, cache=None, renderer=None,
               output_dir=Path("/output")) -> FastAPI:
    """装配 pages-web；依赖全部可注入（测试不需要真实库与网络）。

    ``db_connector`` 仿 ops-web 喂上下文管理器；``cache`` 用 bi-web 的
    CardCache 契约（get_or_compute 自带 per-key 防惊群）；``renderer``
    签名同 ``mart_cli.render_leaderboard_page``，返回 (html, stats)。
    """
    if settings is None:
        settings = load_settings()
    if db_connector is None:
        db_connector = _mart_connector(settings)
    if cache is None:
        from common.bi_web.cache import build_card_cache
        cache = build_card_cache()
    if renderer is None:
        renderer = _default_renderer
    output_dir = Path(output_dir)

    app = FastAPI(title="pages-web")

    def _region_configs():
        """region → cfg；seed 未配/装载失败按可用性 fail-closed（503）。"""
        if settings.region_seed_path is None:
            raise HTTPException(status_code=503, detail="region_seed_required")
        from common.daily_robot.mart_cli import load_region_configs
        return load_region_configs(settings.region_seed_path)

    @app.get("/healthz")
    def healthz():
        # 顶层运维端点（与 bi-web/ops-web 同款）：活性 + mart 连通性。
        try:
            with db_connector() as connection:
                cursor = connection.cursor()
                try:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                finally:
                    cursor.close()
        except Exception as exc:
            _LOGGER.warning(
                "pages-web healthz mart probe failed: %s", type(exc).__name__
            )
            return JSONResponse({"status": "unhealthy"}, status_code=503)
        return JSONResponse({"status": "ok", "database": "ok"})

    def _last_good(filename):
        """磁盘 last-good 兜底；文件也没有才 503（交给 nginx @static）。"""
        try:
            return (output_dir / filename).read_text(encoding="utf-8")
        except Exception:
            _LOGGER.warning("pages-web no last-good file: %s", filename)
            raise HTTPException(status_code=503, detail="unavailable")

    def _current_page(region, cfg, now):
        """当前页：TTL 缓存读穿；渲染失败回磁盘 last-good。"""
        business_date = now.date()

        def compute():
            with db_connector() as conn:
                page, _stats = renderer(
                    conn, region=region, cfg=cfg,
                    business_date=business_date, now=now,
                    output=output_dir / f"{region}.html",
                )
            return page

        try:
            return cache.get_or_compute(
                f"lb:{region}:{business_date}", _PAGE_TTL_SECONDS, compute
            )
        except Exception as exc:
            _LOGGER.warning(
                "pages-web render failed region=%s: %s",
                region, type(exc).__name__,
            )
            return _last_good(f"{region}.html")

    def _archive_page(region, cfg, now, day):
        """存档页：磁盘快路径优先（不占缓存）；缺失才渲染并写存档。"""
        filename = f"{region}{day}.html"
        path = output_dir / filename
        try:
            return path.read_text(encoding="utf-8")
        except Exception:
            pass  # 磁盘缺失才渲染
        try:
            with db_connector() as conn:
                page, _stats = renderer(
                    conn, region=region, cfg=cfg,
                    business_date=date.fromisoformat(day[1:]), now=now,
                    output=path,
                )
            return page
        except Exception as exc:
            _LOGGER.warning(
                "pages-web archive render failed file=%s: %s",
                filename, type(exc).__name__,
            )
            raise HTTPException(status_code=503, detail="unavailable")

    @app.get("/{page}")
    def leaderboard_page(page: str):
        match = _PAGE_RE.match(page)
        if match is None:
            raise HTTPException(status_code=404, detail="not_found")
        region = match.group("region")
        day = match.group("day")
        if day is not None:
            try:
                date.fromisoformat(day[1:])
            except ValueError:
                raise HTTPException(status_code=404, detail="not_found")
        try:
            cfg = _region_configs().get(region)
        except HTTPException:
            raise
        except Exception as exc:
            _LOGGER.warning(
                "pages-web region load failed: %s", type(exc).__name__
            )
            raise HTTPException(status_code=503, detail="unavailable")
        if cfg is None:
            raise HTTPException(status_code=404, detail="unknown_region")

        now = datetime.now()
        html = (
            _current_page(region, cfg, now) if day is None
            else _archive_page(region, cfg, now, day)
        )
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    return app


def serve(application):
    import uvicorn
    uvicorn.run(application, host="0.0.0.0", port=8080)


def main():
    """从环境装配并启动；启动失败只打印一行安全消息（同 ops-web 纪律）。"""
    try:
        application = create_app()
    except Exception as exc:
        print(f"pages-web startup failed: invalid configuration ({type(exc).__name__})")
        sys.exit(1)
    serve(application)


if __name__ == "__main__":  # pragma: no cover
    main()
