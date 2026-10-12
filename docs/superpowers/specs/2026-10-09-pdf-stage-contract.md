# P1：PDF 结构保真与完整链路需求、架构合同

日期2026-10-09。状态：原审核者已批准，六项最终澄清已合入；冻结为P1开发合同。团队和批次交付规则见
`../../04-plan/38-fixed-stage-delivery.md`。本文件收束此前 A/B/C 草案；冲突时本文件与
审核决议优先。用户已授权完成 PDF 及相应架构工作，无需再次请求普通实施许可。

## 1. 用户可见结果和范围

用户能上传文本型、扫描型、混合页 PDF；后台在明确的资源限制下完成解析、结构保存、
分块和索引。检索命中能追溯到正确文档版本、页码和位置；管理端可查看实际解析元素、
表格及质量提示。改变解析器/模型/输出配置后，重建索引应真正使用新解析产物。

本阶段包含：最小可演进 IR、pdfium 基线兼容、Docling 标准 PDF 质量路径、持久解析
身份、基线 OCR 页恢复、只读解析检查、部署准备、真实全链路与质量/资源验证。
不包含：Milvus/Celery 换栈，全部 Office 解析器重写，通用 VLM/图表推理，公式求值，
Excel SQL 计算，用户编辑 IR 的可视化工具，MinerU 集成，配额、爬虫、SDK 新产品功能。
现有格式不能回归；必要的 OpenAPI/SDK 类型再生成属于兼容维护。

## 2. 现状与已接受基础

A1–A3 已由原审核者接受：混合扫描页 OCR 路由、嵌套 Form 四角变换、visual-frame
OCR 布局、原 PDF 引用坐标、完整性不足时保全文和质量提示、跨栏分带、简单宽数值表。
最近独立验证：PDF/OCR70项、ingestion348项、mypy22文件、Ruff通过。不是全项目或完整
PDF质量通过。不得重写这些能力；新增合同需要的兼容修正有回归证据即可。

现有 ParsedDocument/Block/Asset 不是空白项目。保留 Parser 的必需协议
`supported_mimes + async parse(bytes, ParseContext)`，不要强制自定义 Parser 实现新方法。

## 3. 自有 IR 最小合同

共享纯 DTO/验证可放 `core/document_ir.py`，不得导入 owner ORM、Docling、Torch 或网络
客户端。归一化、投影、ID生成等实施逻辑归 ingestion；catalog 经 owner facade 读取。

### 文档与元素身份

- IR schema version；workspace/KB/document/revision/source SHA；parse_version；捕获的
  parser/context fingerprint；producer 版本；明确的质量状态/警告。
- Parser 输出局部节点标识/顺序；pipeline 绑定真实租户/文档身份。parse_version 标识
  实际不可变解析结果，包含输出内容摘要或独立不可变版本；不能仅靠 ordinal 或期望
  parser fingerprint 将非确定输出当作同一结果。摘要使用规范化局部内容，排除最终
  version/element IDs避免循环，排除 PID、时间、临时路径等噪声，再派生最终ID。
- element_id 在同一 parse_version 内唯一稳定；父关系和引用只能指向合法元素，层级
  无环、order 唯一确定。重解析产生新身份；不实现未经证明的跨版本 lineage。
- type 覆盖 text/heading/list/code/table/image/formula；chart/diagram 可作为 image
  子类或未确认分类，禁止在无识别阶段时制造节点关系或数值。

### 位置和内容

- location 是有类型的来源片段列表。PDF 含页号、页面尺寸、明确坐标系、bbox；对多页
  文本按可靠 charspan 分片，span必须注明指向orig还是text并验证范围/覆盖/不重复，
  不把orig offsets套到已规范化text；不复制整段到每一页。无法准确分片则保留一次内容及全部
  provenance用于失败诊断，并明确拒绝不安全投影，该文档不提交新解析成功结果；本阶段
  不建立无精确定位的宽泛降级路径。既有incomplete-OCR-words保全文fallback例外保持
  原有可验证page bbox和显式notice；它不是跨页位置猜测。
- table 保存 rows/columns、每格原始 text、row/col start/end/span、来源、可选类型化
  值/单位。行列数错误也如实保存，不为通过质量测试推断合并成正确答案。
- table Markdown/search_text 由结构派生；跨格文字在 anchor 处写一次；空格、缺值和0
  不混淆。GFM无法表示的跨度保存在 IR，不能丢弃原始结构。
- formula 保存 native原文、可用的识别表达/格式和状态。Docling text空且orig非空时
  保留orig并标记未识别；两者均空保留元素位置和明确缺失提示，不制造LaTeX。
- image 保留受限提取的资产引用及位置、原有图注关系。图形分类/描述未经处理保持未知。
  只保存实际生成的资产；截取/导出不支持时明确asset unavailable，不伪造链接。
- provenance区分native/OCR/model-derived；confidence可空，必须有来源/含义，不能填
  统一可信分。search_text为派生表示，不覆盖结构化content或原始字节。

### 兼容和持久化

首版以带checksum的不可变artifact v2为完整IR权威记录；无需先建立全量element SQL表。
现有Markdown/Blocks通过确定性投影继续给chunker和内容读取接口使用；不得分别维护两份
互相漂移的truth。旧artifact能读取；缺指纹/IR不得被作为新版本结果复用。
资产避免无限内嵌base64，持久化为文档归属对象，IR保存受控引用；限制单项/总字节和
数量。清理覆盖原图裁剪、IR和缓存，旧worker不能在purge后重新生成对象。

## 4. 解析引擎与运行边界

- `pdf_engine=pdfium|docling`，默认保留已部署baseline直至质量评估支持变更；选择
  Docling后缺包/缺模型/执行失败不能静默回退。配置不是文件或请求可指定的shell命令。
- Docling固定已评估2.135.0的standard layout/table路径，cell matching保留true；不引入
  VLM、外部plugins、remote inference。部署准备单独下载并验证固定revision+SHA的资产，
  生成只读manifest；运行时禁止模型下载。实际禁网容器证明，而不只检验环境变量。
- 模型包和Torch位于隔离worker环境/可选镜像，控制/检索服务不能因import加载模型。
  精简PDF环境实验已有，但最终Linux依赖、wheel/model许可清单、CPU镜像均须验证。
- 文档解析在可终止子进程中完成；父任务取消/超时必须回收整个进程树和临时文件。
  输入文件、页数、像素、字符、元素、表格格数、资产/JSON大小、内存和时间有边界。
  起点沿用50MiB上传/500页上限；模型worker的内存/超时单独配置并经实际测试，不能
  生搬pdfium1GiB/120s。建议CPU配置从4GiB/600s起测；不是性能达标承诺。
- 正常解析必须覆盖完整源文件。超页数不能截取前N页再宣SUCCESS；Docling
  PARTIAL_SUCCESS/FAILURE不能提交为完整可检索成功。结构识别不完整与未处理页分别报告。
- OCR使用已验证语言包和本地引擎；扫描件不能在OCR关闭/缺失时被当成成功空文。
  原始页面内容/可引用性与生成图像等可选能力的失败状态分开。

## 5. 指纹、重新解析与人工修改

Registry可选descriptor登记；无descriptor的custom parser仍可解析但禁止结果复用。
内置解析器有明确owned implementation版本；PDF描述引擎包/模型manifest、layout/OCR
版本、traineddata、语言、PSM、render/routing/table等影响输出的配置。路径/PID/并发度
不是身份；模型hash在准备/启动时核验，不对每份文档重复hash整套权重。

context指纹包含language/encoding/json_fields/source_url。一轮attempt开始时捕获一次
描述，worker实际身份须匹配后才提交。旧指纹缺失、不匹配或当前limits不接受缓存时
cache miss；`None==None`不算hit。普通reindex在指纹改变时重parse，同parse/仅embedding
变化时可复用合格IR。一次rebuild要求一致parser部署，不支持混版本无差别结果。

当前人工chunk编辑按ordinal迁移存在错配风险。指纹/IR改变且存在人工修改时，本阶段
禁止猜测套用旧ordinal：返回`REPARSE_MANUAL_EDITS_CONFLICT`，保留旧ACTIVE及编辑，不发布
新索引。旧指纹缺失也不能证明对应；只有原块身份可以证明未变时才沿用编辑保留流程。
相同IR不授权按新ordinal迁移，chunk配置变化而对应不明同样冲突。原索引不能因后台
失败被清空；不实现自动语义合并或全文协调UI。

## 6. 页级恢复、生命周期和进度

基线pdfium→Tesseract路径新增scoped持久OCR cache。只保存成功OcrResult（像素空间
text/words/geometry），key为ws/KB/doc/revision/imageSHA/OCRfingerprint，排除layout版本。
schema、身份、checksum、字符/词/字节/坐标和当前limits都验证；坏/旧缓存视miss。
不跨租户/文档/revision共享，不缓存错误/取消。layout改变可复用OCR再排版。

pipeline向Parser注入绑定当前run的窄cache/progress hook，不授予任意object key权限。
写入前使用catalog/task的lease/revision/recovery fencing与删除互斥，不能只有一次
检查后脱锁等待写对象。cache不是成功登记权威；不设置可被旧worker回滚的latest指针。
progress包含实际completed/totalpages与phase；等待期有heartbeat；不能靠延长timeout
掩盖租约丢失。复用既有队列调度，不引入Celery。

本阶段明确批准现有parse worker内checkpoint/progress/heartbeat和独立OCR并发限制，
替代旧规范的新ocr queue。Docling内部不强行fork：resume_granularity=document，失败
整文重试、成功完整产物可复用；其内部页恢复是后续能力差异，不是本阶段隐藏工程缺口。
Docling长调用可准确显示phase=converting、completed未知并维持heartbeat，不能编造逐页
进度。基线路径必须报告实际completed/totalpages；两种resume能力不能混为同一承诺。

## 7. 最小只读解析检查与授权

扩现有DocumentDetail查看解析状态、引擎/版本、pages、质量warnings，按页/游标查看
元素、表格结构、公式原文和page/bbox。正常预算内的页面预览与bbox overlay属于必需
验收；采用分辨率/像素/总量预算，超出预算明确不可用。预览按需走受限render worker且
受控授权，不允许文档阅读请求无限消耗CPU/内存；必要的已保存图片通过授权端点返回。
实现必须提供preview_max_page_pixels、preview_max_image_bytes、preview_max_document_bytes、
preview_max_pages、preview_timeout_s、preview_max_concurrency、preview_response_max_bytes
等配置并在交付清单冻结实测默认值；不能统一返回unavailable或默默只生成第一页。
IR保存原PDF尺寸/crop/rotation，UI使用对应页面的display transform实现bbox overlay。
不做画布编辑器、自动修正表格或全新管理端。

最终定位合同：DocumentIngestion增加nullable `parse_version_id`（64位hex digest）与必要
索引，commit_parsed和artifact pointer同事务写入；不建全量element SQL表。
`GET /v1/documents/{id}/parse?view=active|candidate`概要默认active；其下elements、
elements/{element_id}、assets/{asset_id}、pages/{page}/preview请求必须显式view与
parse_version，分页cursor绑定两者。由catalog先鉴权`kb:read`且同workspace/未删除，再
校验版本；query-only拒绝。版本不匹配统一404业务码`PARSE_VERSION_STALE`，不暴露其他
租户版本信息，不跳到当前同ordinal。正常找不到解析结果使用明确unavailable业务码。
active只从当前revision及ACTIVE index对应已提交run解析，不将Document.parsed_object_key
当ACTIVE；candidate只从明确BUILDING run取，无结果/有歧义不回退latest。source replacement
已进新revision而ACTIVE仍旧时，返回active parse unavailable并显示处理中/candidate，
绝不将新revision产物配给旧ACTIVE引用。旧NULL版本继续原Markdown/Block读取，不伪造
新element resolution。旧版本无保留定位时stale/unavailable，不承诺永久历史保留。
HTML/Markdown安全渲染，asset id不能变成任意文件/URL抓取入口；response有上限。
共享artifact读取除了checksum，还校验SQL定位记录对应的doc/revision/source/index/
parse身份；不得只信任一个合法checksum就把另一文档内容返回给调用者。

## 8. 质量与完成定义

工程门槛：实现与兼容/安全/恢复/真实链路/静态和回归测试完整，交付可运行准备命令、
镜像和操作者说明。质量门槛独立保留：30份完整原文（五类各六份）、独立表格结构与
阅读顺序/OCR/引用标注、原要求table-structure F1>=0.85、长文档耗时/峰值内存。
阈值原始定义来自M07/旧PDFspike；度量定义及分母必须冻结，不能挑成功页改口径。

已有7 originals+23excerpts不能算30完整；新17原文未标注且7份页映射不一致；无法
redistribute的原件只作local eval，报告许可状态与获取方式。agent visual annotation
必须标为agent-reviewed，不冒称人工专家标注。generated/synthetic fixtures可验行为，
不能替代真实gold。若外部资料不足/分数不达标，交付工程结果及具体失败清单，但阶段
总状态保持quality gate open，root继续集中决定修复，不把任务宣布全部完成。
