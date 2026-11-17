# CycleFlow 机器人样机迭代数据服务

CycleFlow 面向样机研发团队，接收设计修订、部件批次、生产工单和质量试验等独立来源的事件。SQLite 同时保存原始事件身份和派生的业务状态。报表将设计版本、工单、工序耗时、物料批次、试验结果和质量问题连接起来，供团队解释每次迭代的交付效率。

## 运行

依赖版本锁定在 `requirements.txt`。在项目目录运行 `python -m unittest discover -s tests -v` 执行业务测试，`python -m compileall -q cycleflow` 检查源码，`python -m cycleflow.demo` 运行合成数据验收。安装所列依赖后，可设置 `CYCLEFLOW_DB` 为可写的数据库路径，再运行 `uvicorn cycleflow.api:app` 启动 HTTP 服务。命令行帮助为 `python -m cycleflow --db cycleflow.sqlite3 --help`。

演示仅使用临时数据库和本地合成产品。它分两次导入同一清单，验证试验良率，冻结月报，并审计存储的状态链；正常运行无需访问外部服务。

## 事件协议

向 `/events` 提交 `source`、`external_id`、`kind`、带时区的 `occurred_at` 和对象类型的 `payload`。相同事件重放返回既有编号；相同身份提交不同内容返回冲突。缺少前置对象或暂时缺料的事件进入待处理状态，可调用 `/events/{id}/retry` 补齐后重试。其他业务无效事件仍保留拒绝原因。批量接口每次使用稳定的批次编号和完整事件数组；系统核对清单指纹，从已提交的行继续。

事件类型包括 `design.release`、`design.retire`、`lot.receive`、`lot.quarantine`、`job.open`、`job.stage_start`、`job.stage_finish`、`job.scrap`、`plan.create`、`test.record`、`substitute.approve` 和 `substitute.revoke`。设计包含产品、版本、部件数量及可选的父版本。部件批次包含料号、供应商和正整数数量。工单绑定一个设计和产量，成功预留物料后依次经过加工、装配和集成。试验计划绑定精确的产品版本，受测数量必须覆盖该工单全部单位。被接受事件的返回体带 `detail`，给出业务投影结果（如工单的配置与分配、撤销已授权的工单数）。

## 替代件与装配配置

原件短缺时，工程师用 `substitute.approve` 登记替代件决定：绑定一个设计版本和一对 `original_part`→`substitute_part`，带半开有效窗口 `valid_from`/`valid_until`、可选的产量上限 `max_units`、批准人和原因。`valid_from` 不得早于批准时间（禁止倒签），同一设计同一原件的有效窗口不得重叠。`substitute.revoke` 撤销决定但不回溯：已开工的工单保留原记录。

工单开单时为每个需求件解析实际用件：先用原件库存，不足时查该时刻有效的批准改用替代件库存，两者都不够则进入缺料待处理，错误文本说明是无有效批准、批准已过期或撤销、额度不足还是替代件也不足。解析结果（需求件→实际件的完整映射）就是该工单的装配配置，`config_id` 由设计和映射派生，同一设计同一映射永远是同一身份。一个工单只有一个配置，因此良率可以按配置归因。工单可用量预估（capacity）使用同一解析逻辑并返回同一 `config_id`；替代件批次是独立料号库存，绝不计入原件可用量。有效性一律按事件业务时间判定：审批前开出的工单不会因事后批准而改用替代件，晚到的领料事件按其实际发生时间核对窗口。返工工单在开单时独立解析配置，谱系里保留各工单的配置身份。批次隔离对替代件批次同样生效，已隔离批次不能新预留，已被预留的批次拒绝隔离。

## 报表与发布

请求 `/reports/{product}?window_start=...&window_end=...` 获取当前视图。时间窗含起点、不含终点，以工单创建时间决定归属。报表提供按版本汇总、良率、工序时长、等待时长、来源事件编号和质量问题。每个版本条目附 `configurations` 分解：同一版本下每种装配配置各自的工单数、产量、试验良率和所用批准，`mixed_configurations` 标记混配版本——版本级良率只是聚合值，质量差异应对照配置而不是直接归给设计改版。周度、返工分组、瓶颈和供应商视图使用相同的已接受事实。供应商试验归因可以重叠，不能跨供应商直接求和。

追溯链：`/tests/{test_id}` 给出试验对应工单的配置、替代件批准和物料批次；`/substitutions/{approval_id}` 给出批准窗口、已消耗产量和使用它的工单；`/configurations/{config_id}` 汇总该配置下的工单、批次和试验结果；`/jobs/{job_id}/timeline` 与事件血缘携带同一 `config_id`。

向 `/releases` 提交发布编号、产品、时间窗和预览返回的 `expected_fingerprint`。未完成工单或窗口内未解决事件阻止发布。发布后保留原报告和来源事件头；`/releases/{id}/freshness` 比较当前视图与冻结视图，不改写历史。`/quality/audit` 检查事件身份、设计版本链、物料预留、工序顺序、试验记录、发布摘要，以及替代件决定的窗口、范围、产量上限与配置一致性。
