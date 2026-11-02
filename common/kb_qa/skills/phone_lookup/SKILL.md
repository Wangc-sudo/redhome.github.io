---
name: phone_lookup
description: "查公司电信号码台账：手机号/尾号/姓名/平台账号 → 实际持卡人、物理手机、绑定的平台账号。回答「尾号5690 是谁在用」「XX 店铺账号的验证码找谁要」"
tags: [lookup]
order: 9
parameters:
  type: object
  properties:
    keyword:
      type: string
      description: 手机号或尾号 / 姓名 / 平台账号名
  required: [keyword]
---
号码台账精确 SQL 查（dim_phone_number/dim_phone_binding），不走向量（错配=验证码找错人）。
requires_clarification=true 时列出候选请用户选择，不得猜属主。
全员开放，不在 Web 业务收窄面内。
