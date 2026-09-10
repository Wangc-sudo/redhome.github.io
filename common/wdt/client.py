#!/usr/bin/env python3
"""
旺店通旗舰版 API 客户端（复刻官方 PHP SDK WdtErpClient）
=========================================================
2026-09-03 实测验证通过（status 0）。

关键约定（与旧 openapi2 企业版完全不同，勿混用）：
- 入口: http://wdt.wangdian.cn/openapi
- URL 参数: sid / key(=appkey) / salt(appsecret冒号后半) / method / v=1.0
  / timestamp(Unix秒-1325347200, 120秒内有效) / sign / page_size / page_no / calc_total
- body: POST JSON，格式为 [params对象] 的数组（SDK 风格），Content-Type: application/json
- 签名: md5(secret + k1v1k2v2... + secret) 32位小写；
        secret = appsecret 冒号前半；参与签名的参数含 body 字符串（签名后从 URL 移除）
- 分页: page_no 从 0 开始；订单类接口时间窗口 ≤60 分钟需切片（旧经验 50 分钟）
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request

TS_OFFSET = 1325347200  # 2012-01-01 00:00:00


class WdtError(RuntimeError):
    pass


class WdtClient:
    def __init__(self, sid, appkey, appsecret, base_url="http://wdt.wangdian.cn/openapi", timeout=30):
        self.sid = sid
        self.key = appkey
        parts = appsecret.split(":")
        if len(parts) != 2:
            raise ValueError("appsecret 格式应为 'secret:salt'（冒号分隔，来自开放平台应用管理）")
        self.secret, self.salt = parts
        self.base_url = base_url
        self.timeout = timeout

    def _sign(self, params):
        arr = [self.secret]
        for k in sorted(params.keys()):
            if k == "sign":
                continue
            arr.append(str(k))
            arr.append(str(params[k]))
        arr.append(self.secret)
        return hashlib.md5("".join(arr).encode("utf-8")).hexdigest()

    def call(self, method, params, page_size=None, page_no=0, calc_total=0):
        """调用接口，返回解析后的 JSON dict。params 为业务参数 dict。"""
        body = json.dumps([params], ensure_ascii=False)
        req = {
            "sid": self.sid, "key": self.key, "salt": self.salt,
            "method": method, "timestamp": int(time.time()) - TS_OFFSET, "v": "1.0",
        }
        if page_size is not None:
            req["page_size"] = page_size
            req["page_no"] = page_no
            req["calc_total"] = calc_total
        req["body"] = body
        req["sign"] = self._sign(req)
        del req["body"]

        qs = urllib.parse.urlencode(req)
        r = urllib.request.Request(f"{self.base_url}?{qs}", data=body.encode("utf-8"), method="POST")
        r.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(r, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status") != 0:
            raise WdtError(f"WDT {method} 失败: {json.dumps(data, ensure_ascii=False)[:300]}")
        return data

    def call_paged(self, method, params, page_size=40, max_pages=50):
        """分页拉全量：自动翻页合并 data.order / data列表"""
        all_rows, page = [], 0
        while page < max_pages:
            d = self.call(method, params, page_size=page_size, page_no=page, calc_total=0)
            data = d.get("data") or {}
            rows = data.get("order") or data.get("list") or data.get("goods_list") or []
            if not rows:
                break
            all_rows.extend(rows)
            if len(rows) < page_size:
                break
            page += 1
        return all_rows
