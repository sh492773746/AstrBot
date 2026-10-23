# Go 加拿大28功能对照

基线：本机 navsite/backend/internal/modules/pc28，2026-09-22只读核对。运行时没有该项目依赖；不读旧配置、玩家、数据库或接口。

| Go实现 | 独立实现 |
|---|---|
| lottery_fetcher.calculateCanada28 | rules.canada_balls；20个1—80不重复号码校验 |
| room_config_init 六个加拿大房间 | rules.room_defaults；Go分单位转为整数积分 |
| settlement.EvaluateSettlement | rules.evaluate；13/14、019/089顺子、对子/豹子、龙/虎/豹、整数截断 |
| bet_validation 累计限额及coverageMask | Game._place；同一期跨房间全覆盖检查，单笔/玩法/房间累计限额 |
| countdown 210秒、提前20秒 | Game.current；基于真实开奖时间及60秒采集新鲜度 |
| bet_service/settlement/reconcile | Game.place/ingest/settle；单一事务积分账本、唯一订单、冲突保留证据 |
| chase_service | Game.create_chase/tick_chases；多玩法、平倍/翻倍、1—99期、中奖即停、取消 |
| 周/月排行榜及订单历史 | Game.leaderboard及私聊分页入口；北京时间净收益 |
| WebSocket房间界面 | Telegram消息内按钮更新、私聊记录与单独结算通知 |

有意不同：不复制AI预测、BTC28、分分28、虚拟玩家或虚假在线人数。重启不会像旧逻辑重定位遗漏追号后继续下注，而是暂停遗漏计划；失效时间不替换为当前时间；使用不可变订单规则快照；不自动信任第三方备用网址；没有未经确认的免费代理自动发现。每笔订单严格只结算一次。

旧项目源码中暴露的代理字串未复制。Keno代理通过插件敏感后台配置单独提供，默认不从其他服务读取凭据。

测试中的固定案例来自 settlement_test.go，包括50000/50001积分边界；新增事务、权限、广告与生命周期测试使用隔离临时库及模拟Telegram，绝不修改旧服务数据。
