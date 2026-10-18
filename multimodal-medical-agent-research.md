# 多模态医疗诊断 Agent 深度调研与 CAIR 项目设计建议

> 调研日期：2026-10-18  
> 目标：为 CAIR 实习生第二轮面试项目选择可复用的多模态医疗诊断 Agent 设计，并形成 7 天内可实现的方案。

## 结论先行

这道题最值得借鉴的不是某一个“全能医疗大模型”，而是四种能力的组合：

1. **主动取证**：Agent 根据当前假设主动查询患者资料、检查目录、影像和外部知识，而不是把所有材料一次性塞进上下文。
2. **工具增强的影像分析**：VLM 负责理解问题、选择工具和整合结果；分类、定位、裁剪、分割、报告解析等工作交给专用工具。
3. **轻量角色分工**：让文本病史阅读、影像阅读、批评校验和最终综合承担不同职责，但避免固定启动一大组昂贵的 Agent。
4. **可审计输出**：最终诊断必须能够回指到患者文字、检查/序列、影像或工具结果，并保留工具调用轨迹。

对本项目，推荐实现一个**单 VLM 控制器 + 多模态工具箱 + 证据账本 + 校验器**的系统。它比完整复制 MAM 这类多轮多角色框架更适合 7 天交付，同时能覆盖题目中的 VLM、医学影像、可解释轨迹和 SQL 加分项。

这份调研只服务于研究和面试项目，不构成临床诊断系统，也不应直接用于真实患者管理。

## 1. 从附件中提取的项目约束

### 用户请求与附件指令的边界

- **用户请求**：先做深度调研，参考成熟的多模态医疗诊断 Agent 设计，输出一份 Markdown 文档并保存到 `cair` 文件夹。
- **附件中的项目要求**：作为本项目的验收约束，包括 7 天周期、无需训练、必须使用 VLM 支撑 Agent、处理患者入院信息和多种医学影像、可选择接入医疗工具、提交实现/一页说明/示例运行轨迹，以及 SQL 数据访问加分项。
- **附件中的运行配置**：端点和 API key 属于运行时密钥，不应写入调研文档、代码仓库或示例轨迹；实现时通过环境变量读取，并加入 `.gitignore`。
- **“展示思维链”的工程化解释**：提交中应展示可复现的状态变化、工具调用、观察结果、假设更新和失败尝试；不需要也不应泄露模型的隐式逐 token 思维链。

### 当前本地数据的实际形态

对 `data_subset10` 的只读盘点结果如下：

| 项目 | 结果 |
|---|---:|
| 病例 | 10 |
| 检查 | 24 |
| 模态标签 | 11 类 |
| 本地图像 | 3,762 张 |
| 代表性模态 | CT、MRI、X-ray、乳腺摄影、超声、病理、核医学、透视、照片、标注图、插图 |

CT/MRI 序列可能包含几十到数百张切片；`series.json` 提供了模态、序列、视图、切片数和锚点信息。一个可行的 Agent 必须先理解检查层级，再选择少量有信息量的图像，不应直接对所有图像逐张调用 VLM。

`discussion.txt` 是带有真实诊断解释的材料，应只用于离线评估和示例后的对照，不应作为推理阶段输入。`patient_data.txt`、`presentation.txt`、检查目录、`findings.md` 和影像是否全部暴露给 Agent，应在 README 中明确写出，避免评测时发生标签泄漏。

## 2. 值得参考的系统

### 2.1 高度推荐：ClinSeekAgent

[论文](https://arxiv.org/abs/2605.20176) · [代码](https://github.com/UCSC-VLAA/ClinSeekAgent) · [项目页](https://ucsc-vlaa.github.io/ClinSeekAgent/)

ClinSeekAgent 最贴合题目中的 SQL 加分项。它把 Agent 放在原始临床数据之上，让模型自行探索 EHR schema、按时间/关键词/数值检索、执行受限 SQL、查询候选项，再调用医学影像工具；公开实现描述了 16 个 EHR 工具和 6 个影像工具，并强调严格的时间可见性和可追溯工具轨迹。

**应借鉴**：

- 用统一的 `CaseRepository` 抽象文件系统和 SQL 数据源。
- 把“找证据”作为显式 Agent 任务，而不是预先拼接一个超长 prompt。
- 每次工具调用都记录输入、输出、时间和证据来源。
- 为 SQL 设计只读、参数化、表/列白名单和行数上限。

**不必照搬**：完整的 22 工具集合、长程 EHR 和训练轨迹蒸馏对本项目过重；面试实现只需 6-8 个稳定工具即可。

### 2.2 高度推荐：MedRAX

[论文](https://arxiv.org/abs/2502.02673) · [代码](https://github.com/bowang-lab/medrax)

MedRAX 是一个以视觉语言模型为推理骨干、按需调用多个胸片工具的 Agent。公开实现包括视觉问答、分割、局部定位、报告生成、疾病分类、DICOM 处理和可视化等工具，并使用 LangChain/LangGraph 组织动态工具调用。它的 ChestAgentBench 还把检测、分类、定位、比较、关系、诊断和表征拆成多个可评估能力。

**应借鉴**：

- 让 VLM 充当工具路由器和结果综合器，而不是假设 VLM 本身在每一种影像任务上都最强。
- 工具输出优先结构化：标签、概率、解剖部位、坐标、切片号、报告片段。
- 将“定位/比较/诊断”作为不同问题类型处理，而不是只有一个 `analyze_image`。

**对本项目的改造**：不要因为 MedRAX 以胸片为中心就引入一整套胸片模型；先实现通用的模态路由、序列摘要、ROI 裁剪/放大、图像联系表和结构化观察工具。

### 2.3 高度推荐：MedOpenClaw 与 GAZE

[MedOpenClaw 论文](https://arxiv.org/abs/2603.24649) · [MedOpenClaw 项目页](https://jakobshen.github.io/MedOpenClaw/)  
[GAZE 论文](https://arxiv.org/abs/2605.00876) · [GAZE 代码](https://github.com/liamchalcroft/gaze)

这两项工作的共同重点是让 Agent 操作完整检查或查看器，而不是只读取人工挑选的一张 2D 图像。GAZE 使用缩放、窗宽窗位、对比度、边缘检测和文献/相似影像检索，并用 schema 校验输出、记录完整工具轨迹。MedOpenClaw 更进一步把一个完整检查的操作过程记录为可回放 episode，包括 Agent 看到了什么、调用了哪些操作、生成了哪些 ROI/掩膜/坐标/快照以及最终答案提交时有哪些证据。

**应借鉴**：

- 证据对象要保存 `case_id / study_id / series_id / image_id 或 slice_index / tool / observation`。
- 诊断结论和证据引用必须一起生成，不能事后凭空补引用。
- 先做低成本的全局浏览，再做有目标的局部复查。
- 对工具结果做 schema 校验；解析失败时重试或降级，而不是把错误字符串继续传递。

**关键警示**：MedOpenClaw 报告了一个很重要的工程现象：给 VLM 更多专业工具不一定自动变好；如果没有精确的空间定位，工具输出反而可能增加干扰。因此，工具的数量不如工具的输入/输出契约和证据绑定重要。

### 2.4 影像基础设施参考：VILA-M3 / MONAI

[代码与框架](https://github.com/Project-MONAI/VLM-Radiology-Agent-Framework) · [论文](https://arxiv.org/abs/2411.12915)

VILA-M3 的设计重点是把医疗专家模型接到 VLM 上，支持 2D、3D 甚至 4D 信息融合，并让 VLM 根据图像和任务选择合适的专家模型，再把专家结果反馈给 VLM。MONAI 框架中还展示了 CT 分割、胸片专家等集成方式。

**应借鉴**：

- 以 `expert card` 描述每个工具的适用模态、输入格式、输出字段、资源消耗和失败条件。
- 对 CT、MRI 和多视图 X-ray 使用不同的图像预处理和上下文压缩策略。
- 把专家输出当作证据，而不是直接当作最终诊断。

**不建议在 7 天项目中照搬**：本地部署 8B/13B VILA-M3 及其 3D 专家模型会引入显存、环境和模型下载风险；当前任务已有 VLM 端点，更适合先做轻量工具增强。

### 2.5 直接处理 3D CT 的参考：CT-Agent

[CT-Agent 论文](https://arxiv.org/abs/2505.16229)

CT-Agent 把 CT 问题按解剖区域拆分，用解剖独立工具降低复杂度，并用全局-局部 token 压缩捕获跨切片空间关系。这和本地数据的 CT 长序列问题高度相关。

**应借鉴**：

- `study -> series -> view/phase -> slice` 四级结构化，而不是把文件路径直接平铺给模型。
- 全局阶段提供序列级概览；局部阶段只把目标器官/疑似病灶附近的少量切片交给 VLM。
- 使用多平面一致性检查，避免只依据单一轴位切片下结论。

**面试版简化**：可以先使用 `series.json` 的 position/anchor_image_id、等距采样、联系表和简单 ROI 裁剪，不必先实现完整 3D token 压缩模型。

### 2.6 质量控制参考：Radiologist Copilot

[论文](https://arxiv.org/abs/2512.02814)

Radiologist Copilot 把报告流程拆成计划、工具执行、观察/记忆、报告生成和质量控制，并设计了定位、目标区域分析、模板选择、报告质量检查与反馈修订等环节。

**应借鉴**：

- 最终综合前单独运行一个 consistency checker。
- 检查解剖部位、左右侧、模态、时间顺序、否定词和诊断名称是否前后一致。
- 输出“观察到的事实”和“由事实推导的诊断”两个层级，避免把推断伪装成影像事实。

### 2.7 角色分工参考：MAM、MDAgents、MedAgents

[MAM 论文](https://aclanthology.org/2025.findings-acl.1298/) · [MAM 代码](https://github.com/yczhou001/MAM)  
[MDAgents 论文](https://arxiv.org/abs/2404.15155) · [MedAgents 论文](https://arxiv.org/abs/2311.10537) · [MedAgents 代码](https://github.com/gersteinlab/MedAgents)

MAM 将诊断流程分为 General Practitioner、Specialist Team、Radiologist、Medical Assistant 和 Director；MedAgents 采用专家召集、独立分析、报告汇总、协作会诊和最终决策的阶段式流程；MDAgents 则根据任务复杂度选择单 Agent 或群体协作结构。

**应借鉴**：

- 让不同角色拥有不同输入和职责，而不是对同一个 prompt 做无意义的多数投票。
- 对简单病例使用单路径；对多模态冲突、罕见病或高不确定病例才增加专家/批评轮次。
- “Director/Moderator”负责汇总和冲突解决，不直接覆盖原始证据。

**不建议照搬**：固定启动多个 LLM 角色会增加 token 成本、延迟和互相复制错误的风险。对 10 个病例，3 个核心角色已经足够：`Evidence Planner`、`Modality Reader`、`Critic + Finalizer`。

### 2.8 评估与交互参考：3MDBench

[论文](https://aclanthology.org/2025.emnlp-main.1353/) · [代码](https://github.com/univanxx/3mdbench)

3MDBench 用 Patient Agent 模拟多轮问诊，用 Assessor Agent 评估诊断和对话质量；论文报告多模态对话比非对话设置更好，并观察到将诊断 CNN 的预测注入上下文可以显著改善结果。

**应借鉴**：

- 把工具预测当成“供 VLM 审核的候选证据”，而不是直接替代诊断。
- 在评估中分开测单轮、主动取证和带校验器三种设置。
- 对“问了什么、为什么问、问后是否改变假设”保留结构化记录。

## 3. 归纳出的设计原则

### 3.1 先建立病例地图，再开始诊断

输入应先被规范化成一个病例清单：患者基本信息、主诉/表现、可用文本、检查、序列、视图、切片数、图像路径或 URI、是否有标注和时间信息。Agent 先回答“有哪些证据、哪些证据缺失、哪些检查最相关”，再决定调用哪些影像工具。

### 3.2 让图像上下文分层压缩

推荐三层图像上下文：

1. **Study level**：模态、检查目的、序列列表、切片数、时间和可用报告。
2. **Series level**：每个序列的代表切片/联系表/方向和关键参数。
3. **Target level**：针对当前假设的局部裁剪、放大、多平面或相邻切片。

这样既能保留全检查结构，也能避免把数百张切片一次性发送给 VLM。

### 3.3 证据账本比长 CoT 更有价值

Agent 的中间状态可以使用以下结构化记录，而不是输出私有思维链：

```json
{
  "claim": "appendiceal invagination into the cecum",
  "source": "image",
  "evidence_refs": [
    {"study_id": "study-181355", "series_id": "749261", "slice_index": 339}
  ],
  "support": "direct",
  "confidence": 0.86,
  "contradictions": []
}
```

每个候选诊断都应有支持证据、反证/缺失证据、不确定性和来源。最终答案只引用账本中已经存在的证据。

### 3.4 让 Agent 负责路由，让工具负责可验证事实

推荐把以下内容交给工具或确定性代码：文件/SQL 检索、序列排序、图像采样、缩放/裁剪、联系表生成、基础统计、输出 schema 校验和轨迹落盘。VLM 主要负责问题分解、工具选择、跨模态关联、鉴别诊断和最终自然语言表达。

### 3.5 必须有显式停止条件

每轮取证都应检查：当前诊断是否有足够直接证据、是否仍存在关键冲突、下一次工具调用能否减少不确定性、调用预算是否用尽。没有新增信息时停止，避免 Agent 在同一影像上循环。

## 4. 推荐的 CAIR 项目架构

```text
                    ┌─────────────────────────────┐
                    │ VLM Controller / Evidence   │
                    │ Planner                     │
                    └──────────────┬──────────────┘
                                   │ tool calls
        ┌──────────────────────────┼──────────────────────────┐
        │                          │                          │
┌───────▼────────┐       ┌─────────▼─────────┐       ┌────────▼────────┐
│ CaseRepository │       │ Imaging Toolkit   │       │ Knowledge/RAG   │
│ file or SQL    │       │ sampler/ROI/view  │       │ optional        │
└───────┬────────┘       └─────────┬─────────┘       └────────┬────────┘
        └──────────────────────────┼──────────────────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │ Evidence Ledger             │
                    │ facts, refs, confidence     │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │ Critic / Consistency Check  │
                    └──────────────┬──────────────┘
                                   ▼
                    ┌─────────────────────────────┐
                    │ Finalizer                   │
                    │ diagnosis + differentials  │
                    │ evidence + uncertainty     │
                    └─────────────────────────────┘
```

### 4.1 最小角色集合

| 角色 | 输入 | 输出 | 何时运行 |
|---|---|---|---|
| Evidence Planner | 病例地图、用户任务 | 缺口、候选假设、工具计划 | 每个病例开始和需要复查时 |
| Text Reader | 患者资料、表现、findings | 症状/时间/否定项/文本证据 | 默认运行 |
| Modality Reader | 序列摘要、代表图像、局部图像 | 结构化影像观察 | 每个相关检查运行 |
| Critic | 证据账本、候选诊断 | 冲突、缺证、需复查项 | 在最终综合前运行 |
| Finalizer | 经过校验的账本 | 诊断、鉴别诊断、证据引用、置信度 | 只运行一次 |

不要让每个角色都重新阅读完整病例；角色之间传递结构化结果和证据引用即可。

### 4.2 建议的工具接口

```text
get_case_profile(case_id)
list_studies(case_id)
list_series(study_id)
read_patient_text(case_id, fields=[...])
read_study_findings(study_id)
sample_images(series_id, strategy="anchors|uniform|contact_sheet")
crop_or_zoom(image_ref, roi=None, scale=2)
compare_views(image_refs=[...])
record_evidence(claim, refs, support, confidence)
sql_readonly(query_template, params)
finish(diagnosis, differentials, evidence_refs, uncertainty)
```

`discussion.txt` 不应由 `read_*` 工具暴露给推理 Agent；评估器可以在 Agent 完成后读取它并计算结果。

### 4.3 影像采样策略

- **X-ray/乳腺摄影/超声/透视/照片**：通常可以把所有本地图片连同视图 caption 一起发送；先保留视图语义。
- **CT/MRI**：先读取 `series.json`，按序列和方向生成联系表；优先使用 anchor、中心切片和等距采样。只有当 Agent 提出明确的解剖区域或病灶假设时，才进行局部复查。
- **标注图/插图/病理**：将其作为独立 study 输入，标明它是“标注/插图/病理”而不是普通原始影像，避免混淆证据等级。
- **重复研究**：按 study/series 分组，保留检查顺序；若没有时间戳，至少保留目录顺序并在输出中标记时间信息缺失。

## 5. SQL 加分项的落地方式

用同一接口实现两个后端：

```text
CaseRepository
├── FileCaseRepository     # data_subset10 的目录结构
└── SqlCaseRepository      # 只读 SQL 视图/表
```

推荐的最小 SQL 视图：

```text
patients(case_id, age, sex, ...)
encounters(case_id, presentation, occurred_at, ...)
studies(study_id, case_id, modality, purpose, occurred_at, ...)
series(series_id, study_id, caption, count, position, anchor_image_id, ...)
images(image_id, series_id, uri, slice_index, view, ...)
reports(study_id, report_type, text, ...)
annotations(image_id, label, roi, ...)
```

安全边界：

- 只允许 `SELECT` 或预先定义的只读视图；不允许 `INSERT/UPDATE/DELETE/DDL`。
- SQL 参数化，禁止把 Agent 生成的字符串直接拼接到查询中。
- 允许表、列、条件和排序范围白名单；每次查询有 `LIMIT`、超时和最大返回字节数。
- 数据库凭据只在工具服务端，VLM 只能看到经过脱敏和截断的结果。
- 每次查询记录模板名、参数摘要、返回行数和耗时，便于示例轨迹和审计。

这部分可以在演示中用 SQLite 实现：先把当前目录索引成几张表，再用 `SqlCaseRepository` 读取它们。这样既体现 SQL 兼容性，又不需要依赖外部数据库。

## 6. 7 天实现与验证顺序

| 天数 | 交付物 | 重点 |
|---|---|---|
| Day 1 | 数据盘点脚本 + 单轮 VLM baseline | 读取病例、列出模态/序列/图片，确认端点能处理图文输入 |
| Day 2 | `CaseRepository` + 图像采样器 | 文件后端、study/series 分组、CT/MRI 联系表和代表图 |
| Day 3 | 工具调用循环 | `list/read/sample/crop/record/finish`，严格 JSON schema |
| Day 4 | 证据账本 + Critic | 事实/推断分离、冲突检查、停止条件、失败重试 |
| Day 5 | SQL 后端 + 轨迹落盘 | SQLite smoke test、查询审计、可回放 JSONL 轨迹 |
| Day 6 | 消融评估 | 对比单轮、主动取证、主动取证+校验；记录准确率/成本/延迟/证据覆盖 |
| Day 7 | 文档和示例运行轨迹 | 一页说明、一个完整病例轨迹、失败案例和已知局限 |

### 建议的评估矩阵

1. **Text only**：仅患者文字和 `findings.md`，验证文本理解。
2. **Single pass multimodal**：预选代表图像后直接请求 VLM，作为简单 baseline。
3. **Agentic evidence seeking**：由 Planner 选择检查、序列和图像工具。
4. **Agentic + Critic**：加入证据账本、冲突检查和最终 JSON schema。
5. **File vs SQLite**：同一病例、同一工具调用在两个后端得到一致病例事实。

除了诊断是否匹配，还应记录：

- 证据覆盖率：最终诊断是否引用了正确的病例/检查/图像。
- unsupported claim 数量：结论中没有证据支持的医学事实。
- 工具调用次数、总耗时和 token/费用。
- schema 解析失败、重试和超时次数。
- Agent 是否看到了不应暴露的 `discussion.txt`。

## 7. 推荐的最终输出格式

让 Agent 先生成结构化结果，再渲染成自然语言：

```json
{
  "diagnosis": "...",
  "differential_diagnoses": [
    {"name": "...", "why": "...", "confidence": 0.21}
  ],
  "key_findings": [
    {
      "fact": "...",
      "source": "patient_text|study_findings|image|tool",
      "evidence_refs": ["study-.../series-.../image-..."],
      "confidence": 0.88
    }
  ],
  "uncertainty": ["..."],
  "next_best_check": "...",
  "trace_id": "..."
}
```

面试展示时，推荐提供一条完整、可复现的轨迹：病例地图 → 选择检查 → 图像采样 → 结构化影像观察 → 假设更新 → Critic 发现/排除冲突 → 最终诊断。把原始工具输入输出和最终答案分开展示，读者可以逐步检查证据链。

## 8. 最终推荐排序

| 优先级 | 借鉴对象 | 适合本项目的部分 | 建议 |
|---:|---|---|---|
| 1 | ClinSeekAgent | 原始数据取证、SQL、工具轨迹 | 作为数据访问和 Agent loop 的主参考 |
| 2 | MedRAX | VLM 动态路由影像工具 | 作为影像工具箱设计参考 |
| 3 | MedOpenClaw / GAZE | 全检查、局部复查、证据回放 | 作为 trace 和空间证据设计参考 |
| 4 | Radiologist Copilot | 计划-执行-记忆-质量控制 | 作为 Critic/Finalizer 参考 |
| 5 | CT-Agent | 长 CT 序列的全局-局部处理 | 作为 CT/MRI 采样策略参考 |
| 6 | VILA-M3 / MONAI | 专家模型与 VLM 融合 | 作为长期扩展方向，不作为 7 天主线 |
| 7 | MAM / MDAgents | 角色分工和自适应协作 | 只保留少量角色，按复杂度升级 |
| 8 | 3MDBench | 多轮交互和外部预测注入评估 | 作为消融和运行轨迹设计参考 |

## 9. 参考资料

- [ClinSeekAgent: Automating Multimodal Evidence Seeking for Agentic Clinical Reasoning](https://arxiv.org/abs/2605.20176)
- [ClinSeekAgent GitHub repository](https://github.com/UCSC-VLAA/ClinSeekAgent)
- [MedRAX: Medical Reasoning Agent for Chest X-ray](https://arxiv.org/abs/2502.02673)
- [MedRAX GitHub repository](https://github.com/bowang-lab/medrax)
- [MedOpenClaw: Auditable Medical Imaging Agents Reasoning over Uncurated Full Studies](https://arxiv.org/abs/2603.24649)
- [MedOpenClaw project page](https://jakobshen.github.io/MedOpenClaw/)
- [GAZE: Grounded Agentic Zero-shot Evaluation with Viewer-Level Tools and Literature Retrieval on Rare Brain MRI](https://arxiv.org/abs/2605.00876)
- [GAZE GitHub repository](https://github.com/liamchalcroft/gaze)
- [CT-Agent: A Multimodal-LLM Agent for 3D CT Radiology Question Answering](https://arxiv.org/abs/2505.16229)
- [Radiologist Copilot: An Agentic Assistant with Orchestrated Tools for Radiology Reporting with Quality Control](https://arxiv.org/abs/2512.02814)
- [VILA-M3 / MONAI Vision Language Models](https://github.com/Project-MONAI/VLM-Radiology-Agent-Framework)
- [VILA-M3: Enhancing Vision-Language Models with Medical Expert Knowledge](https://arxiv.org/abs/2411.12915)
- [MAM: Modular Multi-Agent Framework for Multi-Modal Medical Diagnosis via Role-Specialized Collaboration](https://aclanthology.org/2025.findings-acl.1298/)
- [MAM GitHub repository](https://github.com/yczhou001/MAM)
- [MDAgents: An Adaptive Collaboration of LLMs for Medical Decision-Making](https://arxiv.org/abs/2404.15155)
- [MedAgents: Large Language Models as Collaborators for Zero-shot Medical Reasoning](https://arxiv.org/abs/2311.10537)
- [3MDBench: Medical Multimodal Multi-agent Dialogue Benchmark](https://aclanthology.org/2025.emnlp-main.1353/)

