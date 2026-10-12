# PDF 独立参考材料准备（不改变 REWORK-01）

日期2026-10-10。开发者仍自主完成REWORK-01，root仅准备后续独立验收材料，未联系开发
插单、未检查开发中的源码、未运行共享DB测试。

原审核者批准：出版方JATS/XML和PubTables官方标注可提供独立于本次候选解析输出的
结构参考。必须区分来源独立、模型训练集无重叠、人工审核三件事；前者不证明后两者。
人工审核gate仍OPEN，当前返工合同/评分规则不变，不把GriTS换算成既定span F1。

## 来源与获取

使用[PMC官方Cloud Service](https://pmc.ncbi.nlm.nih.gov/tools/cloud/)及其公开S3分发，
没有批量抓取文章网页。当前服务以PMCID/version组织成对PDF/XML/metadata；版本号不
保证内容永久不变，故同时记录各文件SHA256并核对元数据提供的MD5。
[官方访问说明](https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/)。

参考目录：`data/pdf-reference-research-20261010/`，与开发/既有corpus目录隔离。
保留官方README、逐篇JSON元数据、原PDF、XML及材料清单。以文章元数据的许可声明
为记录依据，不推断其他文章适用相同条款，不暗示出版方认可本项目。

## 已准备样本

`PMC10009416.1`：官方元数据声明CC BY，10页完整PDF，出版XML含3张表。PDF/XML均
与元数据MD5匹配，本地另存SHA256。XML给出的行数/显式cell数量分别为12/36、11/129、
11/129；后两表有跨格结构，不能用cell数除行数推断列数。

root在未运行Cairn/Docling候选解析前查看第4页渲染：Table1为12行、3列（含表头），
与XML结构相符。已保存`publisher-page-4.png`。核查方式是agent visual check，
仅涉及Table1；没有声称三表均人工审查或该文档所有页已有gold。图像上的约略表范围
为像素x54–660、y647–856（1.2倍渲染），几何标注还需按原页尺寸校正和单独复核。

另取得PMC10000000.1、PMC10000002.1、PMC10000003.1配套文件，官方元数据声明CC0，
XML没有table-wrap，不把它们计为表格结构gold。只是材料获取/文档兼容候选，不按当前
解析器成功与否挑选参考。缺article-title的样本已标为标题缺失，不错误记录为下载失败。
进一步核对三篇均`is_historical_ocr=true`，XML来源已改记`nlm_historical_ocr`，不得
把另一套自动OCR输出当独立人工转写真值。表格参考PMC10009416.1该标记为false。

## 必须保留的参考属性

- reference_source：publisher_xml / pubtables_official_annotation。
- alignment：not_yet_aligned / automatically_aligned / official_alignment，并记录版本。
- review：unreviewed / agent_visual_checked / human_reviewed，范围必须明确。
- training_overlap：unknown，除非有确切训练集证据。
- PDF/XML/参考文件hash、页/region覆盖，已标注为空与未标注严格区分。

[PubTables官方](https://github.com/microsoft/table-transformer#pubtables-1m)提供PDF坐标
和cell/span参考，但来源经过自动对齐/规范化。若后续采用，优先official test split，
仍需核对完整源PDF；裁图不能补足30原文数量，科学论文不能替代缺失的CJK/财报类别。

当前只完成少量成对材料与一张表的agent视觉核查，未运行参考评分，未补足人工gold。
待返工READY后使用冻结评价器验其能否真正处理独立标注，任何改进要求仍按下一次
集中报告/审核/整批任务流转，不在本轮开发中临时追加。

## 其他候选的边界

仅查看了[OmniDocBench官方说明](https://github.com/opendatalab/OmniDocBench#copyright-statement)。
其代码许可和数据用途限制不同；数据声明仅研究、非商业用途。本轮未下载或纳入项目
验收材料，不能因代码为Apache2.0就把其数据当可再分发素材。公开benchmark分数与其
匹配算法也不自动等价于本项目已冻结的span F1；当前以已取得的许可明确材料继续准备。

## 后续补充：Table2 / Table3 来源核查

继续在未运行 Cairn / Docling 候选解析的情况下检查原始 PDF 第5、6页。两页各有一张
横向排放的表，但 PDF 页面的 rotation 实际为0；因此不能把内容朝向直接当作页面旋转。
保留原始页面渲染和仅供阅读的顺时针90度图片，原始坐标不随阅读图片变化。

两张表的出版 XML 均为11行、15列、129个显式单元格，包含多级表头、跨行/跨列表头、
空单元格和正文中间的子表头。按 XML rowspan / colspan 展开后均完整覆盖165个网格位置，
没有交叠或空洞。这只验证参考网格自身一致性，不是候选解析器成绩。

新增 `table2-neutral-reference.json` / `table3-neutral-reference.json`：保留原 XML 文本
及 spans，标记核查范围仅为表位置、朝向、表头分组和正文布局，未逐单元格核对全部文字。
XML 格式化换行保留，不把这些字符串直接宣称为 PDF 字符级真值。几何范围是原始页面渲染
上的近似框，仍需在最终评价器接入前完成坐标对齐，不能作为已认证的引用几何 gold。

`reference-snapshot.json` 固定完整 PDF、XML 和三份参考文件的 SHA256；已重新核验原件
身份与首份参考一致。三表均为出版结构参考、agent visual check、human_reviewed=false，
training_overlap=unknown，quality_gate=false。没有把这些资料加入原24份 corpus，也没有
新增开发任务或将工程返工标为完成。
