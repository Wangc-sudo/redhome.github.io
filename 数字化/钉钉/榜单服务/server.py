# -*- coding: utf-8 -*-
"""
杭州销售完成率榜单 - 后端服务（ECS 部署）
- 纯 Python 标准库（http.server + sqlite3），无需 pip 安装
- GET  /                     前端单页
- GET  /api/leaderboard      最新榜单快照 JSON
- GET  /api/history          当月每日整体完成率序列（趋势）
- POST /api/update           写入快照（Bearer Token 鉴权）

用法:
  python3 server.py [--port 8300]
环境变量:
  LEADERBOARD_TOKEN  推送令牌（POST 鉴权；未设置时拒绝所有推送）
  LEADERBOARD_DB     SQLite 路径（默认 ./data/leaderboard.db）
"""
import json
import os
import sqlite3
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.environ.get("LEADERBOARD_DB") or BASE_DIR / "data" / "leaderboard.db")
TOKEN = os.environ.get("LEADERBOARD_TOKEN", "")
PORT = 8300

DB_LOCK = threading.Lock()


def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS snapshots (
        month INTEGER NOT NULL,
        stat_date TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        payload TEXT NOT NULL,
        PRIMARY KEY (month, stat_date))""")
    return conn


class Handler(BaseHTTPRequestHandler):
    server_version = "LeaderboardSrv/1.0"

    # ---------- helpers ----------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---------- GET ----------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            self._file(BASE_DIR / "static" / "index.html", "text/html; charset=utf-8")
        elif path == "/api/leaderboard":
            month = self._qs("month")
            with DB_LOCK, db() as conn:
                if month:
                    row = conn.execute(
                        "SELECT payload FROM snapshots WHERE month=? ORDER BY stat_date DESC, generated_at DESC LIMIT 1",
                        (int(month),)).fetchone()
                else:
                    row = conn.execute(
                        "SELECT payload FROM snapshots ORDER BY month DESC, stat_date DESC, generated_at DESC LIMIT 1"
                    ).fetchone()
            if row:
                self._json(json.loads(row[0]))
            else:
                self._json({"error": "no data", "message": "尚未推送任何榜单数据"}, 404)
        elif path == "/api/history":
            month = self._qs("month")
            with DB_LOCK, db() as conn:
                if month:
                    rows = conn.execute(
                        "SELECT stat_date, generated_at, payload FROM snapshots WHERE month=? ORDER BY stat_date",
                        (int(month),)).fetchall()
                else:
                    cur = conn.execute("SELECT MAX(month) FROM snapshots").fetchone()
                    m = cur[0] if cur and cur[0] else 0
                    rows = conn.execute(
                        "SELECT stat_date, generated_at, payload FROM snapshots WHERE month=? ORDER BY stat_date",
                        (m,)).fetchall()
            series = []
            for stat_date, generated_at, payload in rows:
                p = json.loads(payload)
                ov = p.get("overall") or {}
                pr = p.get("progress") or {}
                series.append({
                    "stat_date": stat_date,
                    "overall_rate": ov.get("rate"),
                    "total_completed": ov.get("completed"),
                    "progress_pct": pr.get("pct"),
                })
            self._json({"month": (json.loads(rows[0][2])["month"] if rows else None),
                        "series": series})
        elif path == "/healthz":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def _qs(self, key):
        if "?" not in self.path:
            return None
        for kv in self.path.split("?", 1)[1].split("&"):
            if kv.startswith(key + "="):
                return kv.split("=", 1)[1]
        return None

    # ---------- POST ----------
    def do_POST(self):
        if self.path.split("?")[0] != "/api/update":
            self._json({"error": "not found"}, 404)
            return
        auth = self.headers.get("Authorization", "")
        if not TOKEN or auth != f"Bearer {TOKEN}":
            self._json({"error": "unauthorized"}, 401)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            month = int(payload["month"])
            stat_date = str(payload["stat_date"])
            generated_at = str(payload["generated_at"])
        except (KeyError, ValueError, json.JSONDecodeError) as e:
            self._json({"error": "bad request", "detail": str(e)}, 400)
            return
        with DB_LOCK, db() as conn:
            conn.execute(
                "INSERT INTO snapshots(month, stat_date, generated_at, payload) VALUES(?,?,?,?) "
                "ON CONFLICT(month, stat_date) DO UPDATE SET generated_at=excluded.generated_at, payload=excluded.payload",
                (month, stat_date, generated_at, json.dumps(payload, ensure_ascii=False)))
        self._json({"ok": True, "month": month, "stat_date": stat_date})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.log_date_time_string(), fmt % args))


def main():
    if "--port" in sys.argv:
        global PORT
        PORT = int(sys.argv[sys.argv.index("--port") + 1])
    db().close()  # 预建表
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"leaderboard server listening on 0.0.0.0:{PORT}, db={DB_PATH}")
    print(f"push auth: {'ENABLED' if TOKEN else 'DENIED (LEADERBOARD_TOKEN unset)'}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
