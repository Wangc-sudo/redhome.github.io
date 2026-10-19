# -*- coding: utf-8 -*-
"""钉钉真实投递器：群消息 + DING。

* 群消息走 :class:`common.dingtalk.DingTalkClient` 的企业机器人
  ``groupMessages``（``sampleMarkdownDX``，支持 @人）——与现行机器人同一
  发送路径。
* DING 在实测窗口前**保持现行 ``dws ding message send`` 命令契约**（由
  外部 agent 拾取执行），契约文本逐字不变；实测后再决定是否切换工作
  通知 API，切换点就是 ``ding_sender`` 这一个可替换参数。

region 配置（robotCode / openConversationId）以字典注入——容器化后由
Nacos 的 region dataId 提供（阶段 4 Task 4），本模块不读文件、不读环境。
"""

from common.gateway.delivery import DeliveryError


class DwsCommandDingSender:
    """过渡态 DING 投递：输出与现行生产一致的 ``DING_CMD`` 契约。

    契约（core.do_check 现行输出，逐字不变）::

        DING_CMD_START
        dws ding message send --robot-code <rc> --users <ids> --content "<内容>" --type app --format json
        DING_CMD_END
    """

    def __init__(self, emit=print):
        self._emit = emit

    def __call__(self, *, robot_code, user_ids, content):
        cmd = (
            f'dws ding message send --robot-code {robot_code} '
            f'--users {",".join(user_ids)} --content "{content}" '
            f'--type app --format json'
        )
        self._emit("DING_CMD_START")
        self._emit(cmd)
        self._emit("DING_CMD_END")


class DingTalkDeliverer:
    """按 region 配置投递群消息与 DING。

    *client* 是已构造好的 ``DingTalkClient``（凭据由容器注入）；
    *region_config* 形如::

        {"hangzhou": {"robot_code": "...", "open_conversation_id": "...",
                      "group_name": "..."}}

    *relay_resolver*（可选，2026-10-19 测试区）：``region -> str | None``
    改投映射（真源 ``dim_org_region.relay_to``，DB 驱动即改即生效）。
    命中时群消息与 DING 改投目标区域群，标题/内容加
    ``[测试→<原区域>]`` 前缀（用户裁决：带标记一眼可辨）；映射读取
    异常由 resolver 内部 fail-open 按原区域投（宁可发真群不丢消息）。
    """

    def __init__(self, *, client, region_config, ding_sender=None,
                 relay_resolver=None):
        self._client = client
        self._region_config = dict(region_config)
        self._ding_sender = ding_sender or DwsCommandDingSender()
        self._relay_resolver = relay_resolver

    def _route(self, region):
        """改投解析 → ``(目标 region, 是否改投)``。"""
        if self._relay_resolver is None:
            return region, False
        relay = self._relay_resolver(region)
        if relay and relay != region:
            return relay, True
        return region, False

    def send_group(self, *, region, title, body_md, at_user_ids):
        target, relayed = self._route(region)
        cfg = self._region_config.get(target)
        if not cfg:
            raise DeliveryError(f"unknown region: {target!r}")
        if relayed:
            title = f"[测试→{region}] {title}"
        self._client.send_group_markdown(
            cfg["robot_code"],
            cfg["open_conversation_id"],
            title,
            body_md,
            at_user_ids=list(at_user_ids) or None,
        )

    def send_ding(self, *, region, user_ids, content):
        if not user_ids:
            # 无人可 DING 视为投递成功（消息本身已无接收者）。
            return
        target, relayed = self._route(region)
        cfg = self._region_config.get(target)
        if not cfg:
            raise DeliveryError(f"unknown region: {target!r}")
        if relayed:
            content = f"[测试→{region}] {content}"
        self._ding_sender(
            robot_code=cfg["robot_code"],
            user_ids=list(user_ids),
            content=content,
        )
