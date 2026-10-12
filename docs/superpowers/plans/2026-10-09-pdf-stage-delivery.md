# P1 整阶段开发任务书：PDF 结构保真与完整链路

状态：原审核者已批准，最终澄清已合入，冻结并派发。需求权威：同目录上级 specs 的
`2026-10-09-pdf-stage-contract.md`；验收：`../../05-quality/2026-10-09-pdf-stage-acceptance.md`。
唯一实施者 `/root/developer_61`，gpt-6.1-sol/medium。root设计协调和集中验收，原
`/root/design_advisor`审方案及集中问题。开发者不要另起agent，不需逐工作包请求批准。

## 交付方式

这是一个完整开发阶段，以下工作包是内部实施顺序，不是要求逐包向root汇报。先完成
全阶段、自检并形成统一交付报告再通知root。无需等待每个文件审查；routine实现选择
自主处理。只有架构合同矛盾、不可绕过外部阻断、会损坏既有数据或需要不可逆动作时，
提交一次集中阻断说明，继续做不依赖该问题的工作。不把模型推理慢当作需重新问用户。

保留dirty tree和既有工作；无commit/push/发布。现有A1-A3已接受，不重新设计；配额ORM
草稿保留未集成。依赖和重型模型只进入显式PDF worker环境，不进入在线检索进程。
开发阶段你独占disposable集成测试DB，完整产品栈仅cairn-continuation一个。

## W1 — 版本化 IR 与兼容投影

建议文件：`src/cairn/core/document_ir.py`（纯DTO/验证/无引擎），
`src/cairn/ingestion/ir.py`（归一化、局部标识到版本化ID、Block/Markdown投影），
`ingestion/base.py`（可选IR字段兼容原构造）、`ingestion/artifacts.py`（parse payload v2），
必要时`core/parse_artifact.py`共享窄codec，供catalog读而不反向import ingestion。

1. 先固定typed element/locations/tablecells/formula/assets/provenance/quality DTO；
   serialized schema version明确，拒绝非法或循环关系、越界spans、非有限坐标/数值。
2. 表格原始值与类型化值分开，decimal不得转float后声称精确；不计算公式；完整未知保留。
3. parse_version与index_version分离，pipeline绑定owner/source身份。局部节点ID转换成
   parse-version内ID；相同不可变产物复用保持身份，重解析变化产生新身份。
4. 新PDF以IR为权威且统一投影到原Block/Markdown。旧Office/text/custom Parser继续
   工作，legacy包装不发明缺失结构。纯image/formula缺文字但有证据时不能被入口
   `not markdown`简单误判为空；明确检索表示/缺失状态，避免假成功空索引。
5. parse v1读取兼容；只改变parse payload，不无故改chunk/embedding envelope。
   更新所有读者，包括catalog.service.parsed_markdown和前端内容接口，checksum一致。

自检：IR roundtrip、ID/relations、table spans、formula orig、asset bounded refs、legacy
read、旧parser/custom parser兼容、无模型导入、恶意payload稳定失败。

## W2 — 隔离 Docling 质量解析器与可复现部署

建议文件：`ingestion/docling_pdf.py`、`ingestion/docling_worker.py`、
`ingestion/runtime_config.py`、`registry.py`，小型prepare脚本及独立依赖锁/可选Dockerfile。
选用既有process.py并在确有需要时扩展env/进程树取消，不改成shell拼接命令。

1. 写启动/错误/输出规范的测试，再实现adapter子进程调用。选中Docling却未安装、模型
   缺失/指纹不对时稳定unavailable，无silent fallback；选择pdfium仍无Docling依赖。
2. 使用固定2.135.0 standard PDF pipeline，本地artifact path，禁remote/plugins/隐式
   下载；source从字节/受限临时文件进入，不把source_url交引擎自动抓取。
3. 完整页处理和SUCCESS-only；超输入/页数/pixels/元素/表格cells/assets/JSON/内存/time
   显式失败，PARTIAL_SUCCESS不进入READY。清理中间文件及所有子进程。
4. Ordered items归一化IR，span文字只输出一次。公式orig fallback可见。多provenance
   必须准确切分；无安全可验证投影时明确错误，不复制内容/猜第一页。
5. 生成必要的受限页面预览和图像资产，说明分辨率及总量限制；超过可选预览预算须
   明确不可用，不降低正文解析完整性。资产不能带外部URL/任意路径读取。
6. prepare命令固定model revision/hash、最小依赖、OCR语言数据、许可证/NOTICE和worker
   identity manifest。已有模型可复用，不能把ignored eval路径写死进生产配置。
7. 可选worker镜像/环境以非root运行；实测CPU资源，允许配置GPU但不以GPU为首片必需。
   offline验证必须真正禁网。配置/运维doc记录错误码、预算、预热和resume能力区别。

已准备可用资产（无需重新研究）：
- `data/pdf-docling-spike`：docling2.135.0完整评估环境。
- `data/pdf-docling-minimal`：docling-slim[format-pdf,models-local]2.135.0，另需rtree1.4.1、
  opencv-python-headless4.14.0.94；已做原生PDF真实转换，Linux仍需验证/锁定。
- `data/models/docling-2.135.0`：Heron与TableFormer标准模型；固定revision/hash清单见
  `data/acceptance-pdf-20261009/docling-assets-inspection.json`及quality/spike文档。
- 不引入TableFormerV2/MinerU；do_cell_matching=False实测更差，保留true。

## W3 — Parser 身份、缓存失效与真实重新解析

建议文件：registry/base/artifacts/pipeline、catalog ingestion facade及最小migration。

1. 可选registry descriptor旁表，不改变Parser required protocol。None描述仍可parse
   但不可复用；内置版本显式登记，PDF描述完整且由准备/启动核验。
2. capture一次desired parser/context fingerprints；worker实测identity匹配后提交，
   不用结果返回后变更的配置补签。旧artifact缺字段、新parser/context/limit不匹配均miss。
3. `_reuse_parsed`校验doc/revision/source、fingerprints、IR schema、当前limits；模型/
   parser/layout变更真实重parse，仅embedding变更可复用合格parse。
4. 在DocumentIngestion保存parse_version_id（nullable 64位hex digest兼容旧行），commit_parsed
   同事务记录；不新建全量element SQL库。新ID公共编码/长度遵循项目风格。
5. chunk metadata/artifact、vector payload、parent snapshot传播parse_version、element
   IDs、source-local ranges与locations。IR完整内容不全部复制进每个向量点。
6. 人工编辑冲突在可信身份无法证明一致时fail closed。不能继续按新chunk ordinal套
   旧文本；原ACTIVE/编辑保持，错误码REPARSE_MANUAL_EDITS_CONFLICT，失败BUILDING不激活。
   相同IR但chunk配置改变也不能授权ordinal迁移；必须能证明原块身份对应。不做猜测语义迁移。
7. 用户通过既有reindex路径可使已升级parser生效；UI/ops明确如何操作与冲突处理。

自检：旧v1可读但miss、同parse复用、parser/context/配置变化失效、lowerlimit缓存拒绝、
unknowncustom无复用、人工编辑不串段、失败新版本保留旧ACTIVE、激活后引用不漂移。

## W4 — 基线 OCR 页级缓存、任务进度及删除安全

建议文件：`ingestion/ocr_cache.py`、pipeline/runtime/pdf、catalog/ingestion mutation facade、
catalog/maintenance cleanup、必要DTO状态。缓存Protocol只暴露get/put/progress，不暴露
任意ObjectStore或用户构造key。不要往ParseContext塞可序列化凭证。

1. scoped缓存存pixel OcrResult；imageSHA+OCRfp，doc/revision隔离；layout版本不入OCRkey。
2. 校验envelope、身份、checksum、word/text/bytes/dimensions/bbox/currentlimits；
   corrupt/missing→重新OCR，取消/错误不缓存。fallback仍保全文且有notice。
3. cache写和finalartifact写有lease/revision/recovery fence及删除互斥，不能purge后复活。
   限定前缀纳入document/KB purge；重复写结果/释放幂等。现有guard若不适用registered
   阶段，增加窄parse-artifact mutation facade，不误复用embedded-only index guard。
4. 现有parse worker内实现checkpoint/progress+heartbeat，单独配置OCR并发。此次明确
   采用此方案替代新ocr queue；不要注册空ocr handler来声称队列实现。
5. Docling明确document-level retry；不硬fork引擎，不对同页做两套重复OCR。此能力差异
   已审批准，不是本阶段隐藏未完项；长调用显示converting/未知completed和真实heartbeat。

自检：中段故障后前页cachehit、语言/引擎参数变化miss、新layout命中OCR再重排、scope
隔离、坏缓存、cancel/lease lost/deleting KB、purge复试、quota草稿不参与本阶段。

## W5 — 授权解析查看、预览与引用

建议文件：catalog dto/schemas/service/router，纯共享codec，web/api/knowledge.ts，
DocumentDetail.vue及独立ParsePreview.vue，zh/en文案；复用既有组件样式。

1. 最终API以spec§7为准：GET /v1/documents/{id}/parse?view=active|candidate；其下elements、
   elements/{element_id}、assets/{asset_id}、pages/{page}/preview。概要默认active；其他
   请求必须view/parse_version，cursor绑定。active从当前revision/ACTIVE已提交run定位，
   不用Document.parsed_object_key猜测；candidate只取明确BUILDING，无/歧义不fallback。
   先鉴权再版本检查，统一404 PARSE_VERSION_STALE；source replacement过渡态按spec处理。
2. 所有路径验证ws/KB/document状态和kb:read；query-only拒绝；不暴露storage key或
   任意URL读取。上限分页200、总响应受控，资产MIME/nosniff、路径/ID严格验证。
3. UI能查看源revision/parseversion/engine/warnings、页/元素顺序、原grid/spans、公式
   native/recognized状态，以及有预览时的bbox overlay。明确区分PDF原坐标与display
   transform，rotated/cropped页实际框选正确；无资产时有准确说明，不画伪造页面。普通
   验收页在预算内必须有真实PNG和overlay，不能全部返回unavailable。preview像素、单图/
   总字节、页数、deadline、并发与HTTP上限配置按spec§7提供，交付固定实测默认值。
4. legacy artifact展示基本Markdown/blocks及“旧解析缺少结构”的说明，不能因新增UI
   让旧文档内容接口失效。只读，不增加人工编辑IR或复杂画布工具。
5. OpenAPI→web/Python/TS schema再生成。只做合同维护，不扩大SDK功能。

自检：真实浏览器查看普通/扫描/表格/旋转PDF；未授权拒绝、stale版本、删除中、
candidate/active显示、分页换版本、恶意文本安全渲染、预览坐标。

## W6 — 真实数据、质量与性能评价

扩 `scripts/evaluate_pdf_corpus.py` 或独立紧邻脚本，保留旧robustness报告明确用途。

1. 冻结完整原文清单：五类各六份、来源/完整性/许可/sha/pagecount，摘录另列。优先
   复用已有完整文件；失败下载/版本不同明确记录，不把差异页的旧标注平移过来。
2. 独立参考标注结构定义：table cells/spans、reading-order anchors、OCR transcription、
   citation页/框；标注作者/方法/覆盖率独立记录，禁止候选输出直接作gold。
3. 冻结评分定义与手算fixtures，结构分数和cell text错误分开，检测precision/recall
   和coverage计入失败页，未标注项目null不是0也不是通过。原F1>=0.85不能降低；报告
   存在不等于达标，未标注不能从已声明完整门槛的分母里悄悄移除。
4. 同输入跑pdfium/Docling，同模型/资源profile；所有真实失败保留，查看已有错5列表、
   数字重复、公式orig等已知case。截图/表结构/位置人工或agent复核清楚区分。
5. 测完整原文含100页级文本PDF、长扫描件；记录cold/warm、pages/s、p50/p95样本量、
   OCR秒/页、父进程+子进程峰值内存、timeout及输入规模。不能拿单页warm吞吐当100页p95。
6. 资料不足时完成所有可运行指标与报告，列明精确缺口/所需数据；交付状态注明质量
   gate未完成，不擅自降门槛。root验收后统一决定下一批补救工作。

现有证据根`data/acceptance-pdf-20261009`：17新增full originals共615页，另有旧7完整
原件；来源类别/版本与重复需去重，不能直接相加声称30。原30目录有23单页摘录。
`excerpt-mapping-check.json`中7份对应原文渲染不一致，需查版本/重新标注。

## W7 — 阶段自检、运行交付和冻结

1. 源码稳定后跑附带验收矩阵；先focused再全套，源码无变化不反复跑同样矩阵。
2. 真实Linux引擎禁网运行，真实产品upload→parse→IR→chunk→Qdrant→query→citation；
   只用本阶段测试KB，完成purge并证明DB/vector/assets/cache无残留。
3. 回归完整后端和前端；报告每个skip原因。若环境缺失，可启用既有临时服务并补跑，
   不把组合证据写成不存在的一次全绿。import contracts/route authz/schema drift全检查。
4. 运维说明包括准备模型、选择引擎、预算、重新解析、人工修改冲突、页恢复差异、
   故障排查、旧artifact兼容、已知质量缺口、无需联网运行的验证命令。
5. 在`data/acceptance-pdf-stage-20261009/DELIVERY.md`形成统一报告；另给可读文档
   `docs/06-ops/11-pdf-parsing.md`。附文件清单、模型/源码摘要、需求映射、命令/exitcodes/
   XML/截图/质量报告、复现步骤、工程gate与质量gate分开结论。
6. 向root仅发一次阶段ready通知，停止编辑并交出测试DB。root集中验收完形成一份
   findings与reviewer批准的rework plan后再继续下一批修复。
