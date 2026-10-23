# 直付广告投递

私聊只保留“我的群、帮助、客服”键盘。发布广告可发送“广告投递”，或进入“我的群 → 发布直付广告”。

1. 发送群公开链接、@用户名、群ID或名称关键词。
2. 选择匹配群，核对群名、群ID、登记经营者和当前TRC20地址。
3. 确认群后选择广告位，分两步填写广告正文、联系方式，再确认提交审核。
4. 群经营者审核通过后，领取付款单，按其地址、实际金额和有效期支付。

只能选择已登记、经营正常、开放广告位并配置收款地址的群。关键词结果分页，没有全局30群展示上限；私密群可用群名称或群ID查找，邀请链接不作为投放凭据。

准确链接或 @用户名查询失败时，会显示目标群名和群ID，区分「已有登记但尚未核验归属」「已接入但服务未开启」「广告开关未开启」「尚未配置广告位」「广告位全部停用」「未配置收款地址」，并列出对应处理路径。未满足条件不会生成订单或收款。每日娱乐时段不限制广告投递；旧广告时段配置已停止参与准入，不影响已有订单证据。

资金直接进入所选群经营者地址，不充值、不存广告余额；本BOT不持有私钥，不自动转账或退款。公开付款仍受既有本BOT付款开关及扫描健康检查约束，本次不自动开启。

旧充值、余额支付按钮及未完成的充值输入已停止接受新请求。历史余额、订单、到账记录和旧付款监控保留，不能通过删除账本消除既有退款责任。本次不迁移历史本BOT广告套餐至群经营者。

本群玩法设置的“返回群列表”对没有全局玩法权限的经营者返回“我的群”，不要求升级为本BOT管理员。目标群设置仍校验群级权限，返回导航不会新增授权。
# Payment-free delivery acceptance

- Independent-group order details expose "Test delivery settings" only to the
  first platform administrator. The group test switch defaults to off and expires
  after one hour; it must be enabled explicitly for the chosen test group.
- Open a newly submitted order, enable the temporary switch, then confirm test
  delivery. Existing real invoices cannot be converted to test orders.
- Test orders use the normal publishing/pinning queue and visibly contain
  "测试广告·未付款". They create neither chain invoices nor payment claims,
  recharge balances or refunds. The legacy paid flag is a queue compatibility
  flag; `payment_source=test` and the dedicated authorization record distinguish
  it from real receipts.
- Closing the switch blocks unsent test orders; it does not delete already
  published messages. Unknown send results still require review and are not
  automatically resent. Check the saved message ID and publishing status.
- These tests do not validate real TRC20 receipt scanning or authorize public
  payment rollout.
