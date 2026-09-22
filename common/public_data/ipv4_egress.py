"""IPv4-only 出网基线与 IPv6 卡死门禁（2026-09-22，T7 冒烟断点）。

根因：ECS 带 IPv6 地址 → glibc ``getaddrinfo`` 默认 AAAA 优先 → VPC 无公网
IPv6 出网 → 每个公网请求先卡 IPv6 TCP connect（``EINPROGRESS``，实测单次
~120s）再回落 IPv4。全仓库 HTTP 出口统一走 ``urllib`` →
``socket.getaddrinfo``（``pymysql`` 亦动态走 ``create_connection``），
因此进程级一处过滤即可覆盖全部出口，无需改动任何客户端。

默认基线：进程启动时 ``force_ipv4()`` 过滤掉 ``AF_INET6`` 解析结果；
``PUBLIC_DATA_ALLOW_IPV6=1`` 可显式关闭（供未来 VPC 开通 IPv6 出网后切换）。
显式放行 IPv6 时，``live_safety`` 门禁以 ``ipv6_egress_stalled`` 真探测，
「AAAA 优先 + IPv6 出网不通」即 fail-fast，杜绝静默空转数小时。

只影响本进程：RDS/Redis 走内网 IPv4 字面量，不受影响；测试经注入使用
假 getaddrinfo/connect，零外网零 DNS 依赖。
"""

import os
import socket

#: 显式放行 IPv6 的开关；未设置（默认）= 强制 IPv4 基线
ENV_ALLOW_IPV6 = "PUBLIC_DATA_ALLOW_IPV6"

#: 出网探测目标：sync 与 gateway 都必经的钉钉 API 域名（必有 AAAA 记录）
EGRESS_PROBE_HOST = "api.dingtalk.com"
EGRESS_PROBE_PORT = 443

#: IPv6 连通探测超时；足够短——宁可误报快失败，也不复刻 120s 卡死
PROBE_TIMEOUT_SECONDS = 3.0

_TRUE_VALUES = ("1", "true", "yes", "on")


def allow_ipv6(environ=None):
    """True 当且仅当显式设置了 ``PUBLIC_DATA_ALLOW_IPV6`` 真值。"""
    values = os.environ if environ is None else environ
    raw = (values.get(ENV_ALLOW_IPV6) or "").strip().lower()
    return raw in _TRUE_VALUES


def _default_connect(sockaddr, timeout):
    probe = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(sockaddr)
    finally:
        probe.close()


def ipv6_egress_stalled(
    host=EGRESS_PROBE_HOST,
    port=EGRESS_PROBE_PORT,
    *,
    getaddrinfo=None,
    connect=None,
    timeout=PROBE_TIMEOUT_SECONDS,
):
    """True 当且仅当「DNS 返回 AAAA 优先 且 IPv6 出网不通」。

    两步短路：
      1. ``getaddrinfo`` 首个结果族不是 ``AF_INET6`` → False（IPv4 优先，
         无卡死路径；DNS 失败/空结果同样不是本场景，交上层报错）。
      2. AAAA 优先 → 对该 IPv6 地址短超时 connect 探测：通 → False
         （有 IPv6 出网）；``OSError`` → True（卡死场景）。
    """
    getaddrinfo = socket.getaddrinfo if getaddrinfo is None else getaddrinfo
    connect = _default_connect if connect is None else connect
    try:
        infos = getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    if not infos or infos[0][0] != socket.AF_INET6:
        return False
    try:
        connect(infos[0][4], timeout)
    except OSError:
        return True
    return False


def force_ipv4(sock=None):
    """进程级过滤 ``getaddrinfo`` 的 ``AF_INET6`` 结果（幂等）。

    只影响本进程，不改系统配置；DNS 仅返回 AAAA 时得到空列表、立即 loud
    failure，而不是卡死。返回包装后的函数；重复调用返回同一包装。
    """
    sock = socket if sock is None else sock
    current = sock.getaddrinfo
    if getattr(current, "_ipv4_only_baseline", False):
        return current

    def getaddrinfo_ipv4_only(*args, **kwargs):
        return [
            info
            for info in current(*args, **kwargs)
            if info[0] != socket.AF_INET6
        ]

    getaddrinfo_ipv4_only._ipv4_only_baseline = True
    getaddrinfo_ipv4_only._wrapped_getaddrinfo = current
    sock.getaddrinfo = getaddrinfo_ipv4_only
    return getaddrinfo_ipv4_only


def apply_ipv4_baseline(environ=None, sock=None):
    """启动入口：默认应用 IPv4 基线；显式放行 IPv6 时跳过。

    必须在任何 DNS 解析与 HTTP 调用之前执行（门禁探测若启用，须先于本
    函数运行——``live_safety`` 的门禁调用顺序已保证）。返回 True 表示
    基线已应用。
    """
    if allow_ipv6(environ):
        return False
    force_ipv4(sock)
    return True
