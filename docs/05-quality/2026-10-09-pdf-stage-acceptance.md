# P1 集中验收矩阵与问题回传模板

开发者先自检；阶段ready后root按本文件做一次完整验收。原reviewer讨论集中问题和
修复方案。开发途中不逐源码发修改意见。A1-A3已接受的70/348测试证据是baseline，
不能代替阶段新代码的fresh结果。

## 需求—证据矩阵

| ID | 必须行为 | 独立验收/期望 |
| --- | --- | --- |
| IR01 | 版本化自有IR和稳定parse内IDs | roundtrip；非法父/关系/重复ID拒绝；新parse不复用ordinal身份 |
| IR02 | table保留cells/spans；formula保原文 | 金额/0/缺值不混；GFM跨cell不重复；错误grid不伪修；orig不消失 |
| IR03 | v1可读与新IR单一投影 | legacy content仍可读；新blocks/md与IR一致；catalog无反向import |
| PAR01 | 完整原文解析 | 页数一致；partial/超页数不能READY；native/scan/mixed中文覆盖 |
| PAR02 | OCR与几何 | 保持A1-A3回归；Form/crop/rotation；incompletewords保全且notice |
| PAR03 | engine显式选择 | 缺包/模型、hash不匹配稳定失败；不隐式回退/下载 |
| PAR04 | 真实隔离与预算 | Linux禁网；超字节/页/像素/cells/output/memory/time；cancel无残留子进程 |
| REUSE01 | parser/context指纹正确失效 | 同配hit、改engine/model/layout/OCR/context miss、旧无fp miss |
| REUSE02 | 现limits不被缓存绕过 | 降预算后旧产物不超限使用；unknowncustom None不算命中 |
| REUSE03 | reindex与人工修改 | 普通reindex新parser真正生效；旧fp缺失/改变或chunk映射不明时报编辑冲突；旧ACTIVE失败时仍可查 |
| CACHE01 | scoped页恢复 | 第N页失败后前页重试hit；badcache miss；layout变复用OCR；OCR配置变miss |
| CACHE02 | lease/删除安全 | cancel/过期/新revision/删除无stale写；purge含cache/assets无复活 |
| LINK01 | 全链路身份与引用 | IR→chunk→payload→parent→引用均为同parse/source；旧引用不指向新元素 |
| AUTH01 | 原文/IR/assets授权 | 不同ws/无kbread/queryonly拒绝；先鉴权再404 PARSE_VERSION_STALE；无任意对象URL读取 |
| UI01 | 真实解析检查 | 从run定位active/candidate非latest；替换过渡态不串revision；预算内必须真实页图与正确旋转/crop overlay |
| COMP01 | 其他格式与架构兼容 | TXT/MD/Office/custom parsers回归、import boundaries、API schema齐全 |
| QUALITY01 | 完整30原文及独立gold | 类别各6、sha/terms/pages；摘录/agent标注/版本差异单独标注 |
| QUALITY02 | 实际结构/内容质量达标 | 独立gold覆盖足够且冻结规则tableF1>=.85；逐类/逐doc/失败coverage、OCR/order/geometry；有报告但不达标仍失败 |
| PERF01 | 原性能要求实测 | M07§10：典型100页PDF至少120docs/hour/core、100页p95<30s、OCR<3s/page、parse worker稳态RSS<2GB；明确cold/warm/样本数/资源/峰值内存，未达不改目标 |

工程gate为IR/PAR/REUSE/CACHE/LINK/AUTH/UI/COMP全部通过；质量gate为QUALITY/PERF原始
指标及覆盖足够且结果合格。若质量缺口存在，报告工程交付可接受和PDF整体未完成两个
状态；不得将“not measured”或单页通过改写成质量门槛关闭。
30原文齐全与独立gold齐全分别统计。agent-reviewed参考只能支持工程诊断，不冒充原要求
人工gold。性能条款见docs/02-modules/M07-ingestion.md的Performance budgets表（以实际
条款为准）；不要把一次低延迟或单页并发数字换算成完整100页p95。Docling测试配置允许
更大硬内存预算以测明实际行为，但这不等于豁免原稳态RSS指标；二者分别报告。

Docling document-level retry与pdfium/Tesseract page-level OCR resume是已批准的阶段区别；
采用parse worker checkpoint替代独立ocr queue，满足真实进度/heartbeat/恢复要求即可。
工程gate不额外要求深度fork Docling实现内部页恢复，但不得对外声称两者能力相同。

## 运行环境与命令

根目录D:\code\Cairn，PowerShell。危险权限不是删除产品数据的许可。

1. `uv run python -m pytest tests/unit/ingestion -o addopts='' -q --tb=short --junitxml=data/acceptance-pdf-stage-20261009/ingestion-unit.xml`
   加上新增core IR/catalog/identity相关单元测试。
2. `uv run python -m ruff check src apps tests scripts` 与受影响文件format check。
3. `uv run python -m mypy src/cairn apps`。
4. `uv run lint-imports --config .importlinter`。
5. `uv run python scripts/check_route_authz.py`。
6. 集成只使用 `python data/continuation_20260929_tests.py <pytest args>`，它读取临时
   容器凭证，目标PG39231/Redis39222db9/Qdrant39239；一次只有一个test runner。
   相关PDF/ingestion/artifact/catalog/deletion/security实际用例后，source稳定再全后端
   `python data/continuation_20260929_tests.py --cov --cov-report=term-missing --cov-fail-under=80 --junitxml=data/acceptance-pdf-stage-20261009/backend-full.xml`。不降低已有门槛。
7. 在apps/web：`npm test`、`npm run typecheck`、`npm run build`、`npm run api:check`。
8. OpenAPI有变则Python/TS生成checks、SDK兼容测试；不是新SDK开发。
9. 实际产品入口`http://127.0.0.1:38080`，Compose project `cairn-continuation`；禁网
   native/model解析用一次性受限容器，不能启动第二套完整产品栈。不打印env秘密。
10. 浏览器验收使用既有浏览器工具/脚本，记录实际截图和步骤；只前端单测不能代替。

命令以仓库当前脚本为准，若需新增验收runner由唯一开发者实现。所有日志保留exitcode、
测试数、failure/error/skip和对应源码身份；报告不能只贴PASS字符串。

## 交付目录约定

`data/acceptance-pdf-stage-20261009/` 保存 DELIVERY.md、manifest、测试XML/log、
模型和依赖身份、browser screenshots、真实sample报告、cleanup证明。不得包含明文key。
已存在`data/acceptance-pdf-20261009/`作为历史A阶段和candidate对照，避免覆盖旧证据。

## 一次性问题报告模板

每个发现以以下字段记录，同类根因合并，不拆成几十条临时消息：

```
ID / 关联需求 / 严重度
影响：用户会看到什么错误，是否丢文、错引用、越权或只是质量不足
前置环境与输入身份：源码、模型、文件SHA、配置（无秘密）
复现：可执行命令或明确浏览器步骤
预期 / 实际
证据：测试输出、原文截图、IR/索引差异
根因：已证实或待证实分别标明
建议方案和兼容/迁移影响
必须补测及回归范围
审核者结论
```

root完成本轮矩阵后提交 `FINDINGS-01.md`；与同reviewer确定后写
`REWORK-01.md`（文件/接口/行为/测试/验收），再一次发开发者。修复期间不补充无关功能。
若出现严重数据/授权缺陷需提前停止某项验收，记录停止理由，不用错误状态继续生成
不可信通过证据。其余测试可独立完成。
