# 分组卡密：一套多行账号资料 = 一个库存

在「卡密库存 → 添加卡密」选择 **分组卡密**。同一套资料中可以有多个账号、字段和空行，不会按行拆库存。不同套之间用独立一行的 `======== CARD ========` 分隔。

以下示例保存后库存为 **2 组**（示例账号不是真实凭据）：

```text
--- UPDEVNEWS MAIL ---
Username: account1
Email:    account1@example.com
Password: xxx

--- PROTON ---
Email:    mail1@example.com
Password: xxx

--- EPIC ---
Account:  mail1@example.com
Password: xxx

======== CARD ========

--- UPDEVNEWS MAIL ---
Username: account2
Email:    account2@example.com
Password: xxx

--- PROTON ---
Email:    mail2@example.com
Password: xxx

--- EPIC ---
Account:  mail2@example.com
Password: xxx
```

在商品发货策略中绑定这个库存，设置「每购买 1 件发货份数」为 **1**。需要买 N 件发 N 套时，在商品列表开启「多数量」。原有发货规则也可以绑定分组卡密。

买家购买一份时收到第一整套资料，保留组内换行、空行、缩进和字段空格；分隔符不会发送。库存扣减 1 组，下一位买家领取下一组。已发货记录也是整套一条，并包含订单关联信息。记录沿用现有扣库存时写入的机制，不代表平台消息已确认送达。

没有分隔符的多行内容只算 1 组。首尾、重复分隔符产生的空组不计库存；分隔符前后允许空格。分隔符必须独占一行，正文里包含这段文字不会切分库存。组外空白行会清理，Windows 换行会统一为换行符。

补充库存时，编辑库存，在最后一套后新增分隔符，再粘贴新资料。已发出的资料不要重新粘回剩余库存。

原有 **批量数据** 仍是一行一个库存；**固定文字** 仍是每次发送同一段文本。已经按行拆开的账号资料，需要重新整理为完整套后使用分组卡密。

后端启动会自动升级旧数据库的卡密类型约束，保留已有字段、卡密 ID、库存、模板与发货绑定。没有新增数据列。前端源码修改后运行 `cd frontend`、`npm ci`、`npm run build`，然后从仓库根目录运行 `python Start.py`；已有服务需要重启并刷新后台页面。
