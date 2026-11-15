# 本地接口约定

所有接口使用 UTF-8 JSON，错误响应为带 error 字段的对象。GET /health 返回进程健康状态。POST /imports 接收 request_key 与 data，data 遵循业务资料契约；新导入返回 201，完全相同的请求重放返回 200。相同请求号对应不同资料返回 409；非法资料返回 400，不留下部分版本。

GET /revisions 列出 revision 和内容摘要。GET /records 返回业务原始记录；GET /summary 返回已有概览，两者都返回 revision、digest 和 result。可加 revision 查询参数固定历史资料，不提供时读取最新资料；资料不存在为 404。摘要对对象键顺序不敏感，对数组顺序敏感；资料内容相同可共享版本，导入请求回执相互独立。

## 分析接口

GET /uncertainty 返回不确定性分析，包装字段同样为 revision、digest、result，可用 revision 参数固定到任一历史版本。result 中：

- total 与 products 给出中心值、可得的硬界（hard_low/high_kg）以及仅在「不同来源相互独立」假设下成立的 sd；来源间相关性未知时另给 sd_range_unknown_correlation_kg 的可行界。区间来源与分布来源混合时总量没有有限硬界，相关字段为 null。
- sources 按因子 source 分组，每组只有一个共享驱动（shared_driver），同来源的多行活动不被独立化；每行给出杠杆比例与来源级界宽或 sd。
- information_gaps 列出缺因子活动、仅给区间的来源和方差贡献来源，并按补测价值排序（区间按半宽、normal 按方差），每条给出最值得先测的活动与潜在收益口径。
- unsupported_claims 明确哪些风险数字不被资料支持：区间不支持分位数和覆盖率；混合来源不支持整体硬界或覆盖率；独立假设下的 sd 与 covariance 是条件结果而非承诺。

GET /comparison 比较两个不可变版本，查询参数 from 与 to 均为 revision；省略 from 时取上一版本、省略 to 时取最新版本，不足两个版本时返回 400。响应包含 base_revision/base_digest 与 target_revision/target_digest，result 中：

- bridge_total_kg 与 product_bridge 把已知总量变化分解为 factor_effect（因子依据修订）、allocation_effect（分摊依据修订）两项资料修订效应，以及 activity_effect（活动量变化）、added_effect、removed_effect、relink_effect（因子重新挂接）四项经营活动效应；各项之和与新版本中心值对账，reconciliation_ok 为 true 时桥接闭合。
- factor_revisions 逐因子列出变化字段；activity_changelog 逐活动列出变化类型。
- uncertainty_change 两端各自按当时资料独立计算，旧版本不会被新因子覆盖。
- 缺因子活动只在 missing_factor_activities 与 changelog 中体现，不进入中心值桥接。

未知路径为 404。现有客户端依赖这些路径和包装字段；后续增加业务分析接口时保留既有行为，不能把资料导入成功等同于业务分析已经形成结论。每个分析产物都声明所用数据版本（revision 与 digest），客户端保存和对外引用声明时必须同时保存这两个字段，不能在读取时偷偷替换成最新资料。
