# PDF P1 集中审核报告

日期：2026-10-10。审核者：原 `/root/design_advisor`。协调者已接收报告并抽查关键源码。
结论：暂不接收工程 gate；质量 gate 保持 OPEN。本文件是阶段审核结果，不是开发返工
指令，也不代表 root 已完成独立整体验收。开发者继续保持交付冻结。

审核者阅读四份冻结合同、DELIVERY及主要实现，独立跑4份无DB测试12项通过，并用内存
样本复现下列问题；没有修改代码、联系开发或使用共享integration DB。root已源码核对
跨页first-location、双投影、escaped-pipe split、进程清理条件、预览限流位置和未接通
评价器；本轮未将审核者的复现冒充root重新跑过的集成结果。

## 证据边界

- 后端1371通过/82.22%先于最后两个cache guard改动；15unit+2integration为补充证据，
  不是最终源码整套重跑。root此前已独立核对XML计数。
- 24完整原文/741页，独立gold0，不能关闭质量gate。
- Docling corpus2成功/22失败的长跑跨源码变化，只作诊断；最终产品4/5成功、旋转样本
  PARSE_EMPTY_CONTENT是失败。harness exit0只代表报告跑完，不代表所有样本成功。
- 交付报告对此基本如实披露。工程拒绝接收的理由是下面具体缺陷，不是重复计算质量缺口。

## 高优先级工程问题

### R01 不安全跨页表格投影（IR02/PAR01/LINK01）

位置：ingestion/ir.py project_document；core/document_ir.py Element验证。
审核者构造两页table、两行文字且无cell/page provenance，经bind→project被接受，整表
输出到page1：`[(1, '| page1 |\n| --- |\n| page2 |')]`。源码对table/image执行
`parts=parts[:1]`，多locations未阻止错误的公开page/bbox。

方案：有可靠cell/row来源才分片，否则PDF_UNSAFE_PROJECTION；跨页image/caption也不能
默认第一页。补测无provenance拒绝、合法分页不重复/遗漏、失败不激活BUILDING且旧ACTIVE保留。

### R02 content接口存在第二套投影且缺目标身份绑定（IR03/LINK01/AUTH01）

位置：catalog/service.py document_content与parsed_markdown。
已复现：两页orig=FIRSTSECOND、span分别FIRST/SECOND、text=normalized differs，索引正文
为`FIRST\n\nSECOND`，content接口却为`normalized differs`。前者按project_document，
后者直接连接element.search_text。旧content路径也未像read_parse核对SQL记录的完整
document/revision/source/index/parse身份；合法checksum不能证明文件属于已授权目标。
身份问题是源码确认，未通过共享DB注入错误指针。

方案：共享纯、确定性投影与受验证读取合同；旧content也传预期身份并验证。补测
orig/text跨度差异、element数组与order不同、合法checksum但owner/revision/source不符。

### R03 转义竖线被拆成额外表格列（IR02/COMP01）

位置：ingestion/ir.py _baseline_table。
已复现两列`| A\|B | C |`和`| D\|E | F |`经过IR包装变成三列，因为先split('|')再
unescape。新增IR改变既有parser已正确生成的内容。

方案：escape-aware解析；无法安全恢复结构则保留原table文字，不发明错误grid。补测
pipe/backslash/HTML escape/换行/空格/零值，覆盖baseline PDF与Office producer。

### R04 合法部分越页文字在IR绑定时回归失败（PAR02/COMP01）

位置：core/document_ir.py Location；ingestion/ir.py bind_document。
已有PDF fixture文字从x=-3开始，PdfParser成功、bbox约(-2.952,717.492,132.299,728.592)，
随后bind被“location must lie within its PDF page”拒绝。旧测试只到Parser未覆盖IR。

方案：区分保真的source bbox和页面相交的display bbox，保留原证据并正确裁剪展示。
补测MediaBox/CropBox部分越界parse→IR→chunk成功；非法/nonfinite几何仍拒绝。

### R05 父进程先退出时孙进程未清理、超时可无界等待（PAR04）

位置：core/native_process.py run_bounded finally。
审核者复现parent启动继承pipes的child后立刻退出，child睡1秒，设timeout0.1秒却约
1.143秒才返回。无限存活child会使pipe drain无限等EOF。源码只在leader returncode
为None时kill tree，随后_discard/process.wait无cleanup上限。

方案：异常/取消/overflow按process group或平台job生命周期清理，不以leader活着为前提；
最终drain/wait有明确上限。补测parent先退、grandchild持pipe、正常树取消、overflow、
Linux真实树及Windows支持边界。不是增加通用sandbox。

### R06 预览并发/deadline未覆盖大对象读取与排队（UI01/PAR04）

位置：catalog/parse_view.py preview_page。
源码先read_parse（最多256MiB artifact）、再读最多50MiB source，最后进入semaphore；
timeout仅给child。多个请求可在slot前占大量内存，排队无总期限。

方案：大读取前有界准入；排队、读取、验证、render和cleanup有明确预算。补测slot已占
和多个等待者、读取不会提前堆积、等待有界、取消释放slot，正常PNG/overlay保持通过。

## 中优先级、工程合同仍必须修复

### R07 parse identity未校验派生ID与语义元数据（IR01）

位置：core/document_ir.py DocumentIR验证。
已复现任意更换64hex element_id而parse_version不变仍接受；改变warnings/pdf_pages
metadata也不改变验证结果。digest排除ID后没有校验规定的派生关系，又过宽排除metadata。

方案：唯一localID与最终ID派生关系校验；geometry/provenance/quality等语义证据参与摘要，
只排除明确runtime noise。补测ID、重复localID、语义状态变化被拒绝/新版本，PID耗时不影响。

### R08 当前资源限制的复用与引擎执行校验不完整（REUSE02/PAR04）

位置：ingestion/registry.py accepts_cached及docling_worker/docling_pdf。
已复现含600×800 OCR页面的IR在max_page_pixels=1时仍允许cachehit。cached校验覆盖
部分字段但缺pixel/word/layout/结构事实；Docling部分预算写死而非完整执行配置。

方案：各engine实际适用预算统一校验，保存必需事实，缺事实miss；区分native无需render
与实际OCR/render页面。补测降低pixels/pagechars/elements/cells/output后的复用与错误码。

### R09 Docling OCR有效配置与实际身份不一致（PAR03/REUSE01）

位置：ingestion/docling_worker.py与registry.py；worker-manifest-linux.json。
descriptor记录主进程OCR语言/Tesseract身份，worker却固定eng/chi_sim/chi_tra；最终manifest
没有Tesseract binary/version/traineddata。因此配置变化不保证作用于worker，实际OCR资产
变化不保证被producer身份捕获。

方案：传入并执行有效OCR配置，准备/运行核验实际OCR binary/version/traineddata；仅支持
固定值时明确拒绝其他配置。补测语言参数实际变化、资产变化失效及no-runtime-download。

## Q01 评价工具未接通与数据缺口须分别处理

scripts/evaluate_pdf_stage.py没有独立annotation输入接口；structure_score未接到corpus
流程，OCR CER/order/geometry直接写null。因此提供gold后也还不能完成完整评分。
除6份原文/独立gold不足，还存在评价器实现未完成。补通指标后，最终benchmark须冻结
源码与模型；保留全部失败，不筛成功项缩分母。无需在工程红测前反复跑昂贵模型。

## 可保留的实现与下一步

显式engine/no silentfallback、ACTIVE/candidate主体授权、scoped cache与mutation guard、
删除前缀、手工编辑身份核验方向、Vue转义/overlay、两引擎不同恢复粒度均有实质实现，
无理由推倒重写。commit_chunked仍有ordinal lookup需集中补测与前置验证一致；未复现
风险不计为已确认高优先级缺陷。

root完成独立验收后合并所有证据，原reviewer确认REWORK-01，再一次性交开发。建议分组：
R01–R04统一IR/读取保真；R05–R06生命周期预算；R07–R09不可变身份/有效配置；Q01评价器。
每组先能复现原问题的测试再修代码，整批完成后统一回归真实Linux/product/citations。
