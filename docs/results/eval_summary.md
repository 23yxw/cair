# Evaluation summary

## 评测设计

同一批 10 个匿名病例（`case-01`..`case-10`）、同一个模型（`Qwen3.6-35B-A3B`，`temperature=0`，`seed=7`）。每次只改一个条件，用来分开看：主动取证有没有用、Critic 有没有用、放射报告会把答案抬高多少、换 SQL 后端会不会改结果。

推理阶段一律看不到 `discussion.txt`，也看不到病例文件夹名。`findings.md` 只在 S4 开放。评分发生在该次运行结束之后：另一次 LLM 调用阅读目录名派生的参考诊断和 `discussion.txt`，给 `exact` / `compatible` / `wrong`。Agent 自己的轨迹里不能出现这些标签；`leak-free` 检查的就是这件事。

| 设置 | 运行方式 | 放射报告 | 数据后端 | 要回答的问题 |
|---|---|---|---|---|
| S1 | 一次调用。入院文本 + 每个序列的代表切片（短序列整组放入，长序列只放 anchor），没有工具、没有账本 | 隐藏 | 文件 | 不做 Agent 时，模型一次性能到什么程度 |
| S2 | 完整工具循环：病例地图、自行采样、证据账本、`finish`。没有 Critic / Finalizer | 隐藏 | 文件 | 主动选择检查和切片，比一次看完强多少 |
| S3 | S2 + 只看账本的 Critic（最多一轮有限复查）+ Finalizer。主线系统 | 隐藏 | 文件 | 定稿前的一致性检查能不能改变诊断 |
| S4 | 与 S3 相同，并注册 `read_study_findings` | 可见 | 文件 | 已有放射报告时的上限。多份报告直接写出诊断，不能与 S1–S3 公平比准确率 |
| S5 | 与 S3 相同 | 隐藏 | 只读 SQLite | 同一套工具换数据源后，病例事实和诊断是否还一致 |

每例默认预算：最多 14 次计费工具调用（`record_evidence`、`update_hypotheses`、`finish` 不计），最多 24 张图，单次 LLM 超时 120 秒，单例超时 15 分钟。

## 评测基线

**基线是 S1。** 后面的设置都和它比，而不是和某个外部数据集或公开榜比。

S1 把材料一次交完：入院文本，加上代码预先选定的图像。序列不超过 6 张时全部放入；CT/MRI 等长序列只放 `series.json` 里的 anchor 切片。图像仍标成 `img:<id>`，所以「无支持声明」对 S1 同样可算。它没有工具循环，`calls` 恒为 0，`stop` 为 `single_pass`。

读表时：

- S2 − S1：主动取证带来的诊断变化，以及多出来的 token 和工具调用。
- S3 − S2：Critic 和 Finalizer 带来的变化。本次 S3 的 exact 与 S2 相同，Critic 在 10 例里都选择直接定稿。
- S4 不是更强的基线，而是「报告可见」的上界。
- S5 − S3：后端差异。两边病例事实由单元测试保证逐字段相同；诊断分档不完全相同，来自同模型重复运行的波动。

## 指标字段

汇总表是 10 例的合计或平均。分项表是单例。

**汇总表**

| 字段 | 含义 |
|---|---|
| setting | S1–S5，定义见上 |
| n | 病例数，这里恒为 10 |
| exact | 主诊断与参考疾病实体相同的例数。同义词、缩写、更具体且正确的亚型算 exact |
| compatible | 大类或机制正确，但没点出参考实体的例数。例如「无骨转移」对上「骨赘假阳性骨扫描」可以落在这一档，由评分模型判定 |
| wrong | 另一种疾病的例数。三档之和等于 n |
| top3 hit | 参考诊断出现在主诊断或列出的鉴别诊断中的例数。主诊断已是 exact 时记为命中 |
| unsupported claims | 关键发现里，引用了本次运行没有看过的 id 的条数，10 例相加。0 表示账本拒绝了未见引用 |
| primary study cov. | 每个病例「取过图的 primary 检查数 / 该病例 primary 检查数」，再对 10 例取平均。1.00 表示每个原始检查至少看过一张图 |
| mean tool calls | 计费工具调用次数的平均。S1 没有工具，为 0 |
| mean images | 发给模型的图像份数的平均。一张联系表算 1 份 |
| mean tokens | prompt 与 completion token 之和的平均，含该次诊断里的 Critic 与 Finalizer，不含事后评分调用 |
| mean s | 单例墙钟时间的平均（秒） |
| degraded | Finalizer 三次都写不出合法 JSON、改用账本模板报告的例数 |
| leak-free | 轨迹中不含原始文件夹名、也不含 `discussion.txt` 里长度超过 40 字符的句子的例数，写成 `通过数/n` |

**分项表**

| 字段 | 含义 |
|---|---|
| case | 匿名病例 id。编号按数据目录名排序，不是诊断 |
| reference | 由文件夹名去掉末尾数字得到的参考诊断，只给评分用 |
| diagnosis | Agent 的主诊断原文 |
| grade | 该例的 exact / compatible / wrong |
| top3 | 参考诊断是否出现在主诊断或鉴别诊断中：y / n |
| calls / imgs / tokens | 该例的计费工具次数、图像份数、诊断过程 token |
| stop | 结束原因。`single_pass` 仅 S1；`finish` 为正常提交；`finish_after_revisit` 为 Critic 要求复查后再次提交 |
| critic | 只在 S3–S5 有值，格式为 `决策/问题条数`。`finalize/0` 表示没有问题并直接定稿；`revisit/2` 表示提出 2 条问题并要求再看 |

| setting | n | exact | compatible | wrong | top3 hit | unsupported claims | primary study cov. | mean tool calls | mean images | mean tokens | mean s | degraded | leak-free |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| S1 | 10 | 5 | 0 | 5 | 7 | 0 | 1.00 | 0.0 | 7.2 | 14327 | 160 | 0 | 10/10 |
| S2 | 10 | 6 | 0 | 4 | 6 | 0 | 1.00 | 11.0 | 8.3 | 77408 | 85 | 0 | 10/10 |
| S3 | 10 | 6 | 0 | 4 | 7 | 0 | 1.00 | 10.3 | 6.6 | 81426 | 135 | 0 | 10/10 |
| S4 | 10 | 7 | 1 | 2 | 9 | 0 | 1.00 | 11.6 | 5.4 | 75285 | 134 | 0 | 10/10 |
| S5 | 10 | 6 | 1 | 3 | 6 | 0 | 1.00 | 11.0 | 9.7 | 90418 | 141 | 0 | 10/10 |

## S1

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis with periappendiceal abscess | wrong | n | 0 | 6 | 12796 | single_pass |  |
| case-02 | breast fibromatosis | Desmoid-type fibromatosis | exact | y | 0 | 7 | 15783 | single_pass |  |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Bone metastasis from bladder cancer | wrong | n | 0 | 4 | 12481 | single_pass |  |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic diverticulum | exact | y | 0 | 2 | 9802 | single_pass |  |
| case-05 | extensive multi vessels coronary artery disease | Giant Right Coronary Artery Aneurysm | wrong | y | 0 | 13 | 13819 | single_pass |  |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 0 | 5 | 8310 | single_pass |  |
| case-07 | patellar tendon rupture | Anterior Cruciate Ligament (ACL) tear | wrong | y | 0 | 7 | 11000 | single_pass |  |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Congenital Adrenal Hyperplasia (CAH) with ambiguous genitalia and hydrometrocolpos | wrong | n | 0 | 16 | 20775 | single_pass |  |
| case-09 | slipped capital femoral epiphysis scfe | Slipped Capital Femoral Epiphysis (SCFE) | exact | y | 0 | 4 | 17119 | single_pass |  |
| case-10 | spondylolisthesis with follow up | Dysplastic spondylolisthesis secondary to hemisacrum | exact | y | 0 | 8 | 21385 | single_pass |  |

## S2

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis | wrong | n | 11 | 21 | 129133 | finish |  |
| case-02 | breast fibromatosis | Fibromatosis (desmoid tumor) of the breast | exact | y | 12 | 7 | 59093 | finish |  |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Bone Metastasis (Sclerotic) | wrong | n | 10 | 6 | 69949 | finish |  |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic diverticulum | exact | y | 5 | 2 | 44294 | finish |  |
| case-05 | extensive multi vessels coronary artery disease | Severe coronary artery disease (atherosclerosis with extensive calcification) | exact | y | 13 | 13 | 116560 | finish |  |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 9 | 5 | 51455 | finish |  |
| case-07 | patellar tendon rupture | Patellar tendon rupture | exact | y | 14 | 10 | 64963 | finish |  |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Disorder of sexual development (DSD) with associated skeletal anomalies (likely congenital adrenal hyperplasia or chromosomal DSD) | wrong | n | 14 | 7 | 93415 | finish |  |
| case-09 | slipped capital femoral epiphysis scfe | Bilateral slipped capital femoral epiphysis (SCFE) with surgical fixation | exact | y | 9 | 4 | 54928 | finish |  |
| case-10 | spondylolisthesis with follow up | Adjacent segment disease at L4-L5 following L5-S1 fusion | wrong | n | 13 | 8 | 90294 | finish |  |

## S3

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis | wrong | n | 11 | 9 | 106064 | finish | finalize/0 |
| case-02 | breast fibromatosis | Fibromatosis (Desmoid Tumor) of the breast | exact | y | 12 | 7 | 68276 | finish | finalize/0 |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Localized bladder cancer without evidence of bone metastases | wrong | y | 9 | 5 | 92413 | finish | finalize/0 |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic Diverticulum | exact | y | 5 | 2 | 34392 | finish | finalize/0 |
| case-05 | extensive multi vessels coronary artery disease | Severe multi-vessel coronary artery disease | exact | y | 12 | 12 | 87501 | finish | finalize/0 |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 9 | 5 | 54123 | finish | finalize/0 |
| case-07 | patellar tendon rupture | Complete patellar tendon tear | exact | y | 10 | 6 | 107626 | finish | finalize/0 |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Currarino syndrome | wrong | n | 13 | 8 | 136114 | finish | finalize/0 |
| case-09 | slipped capital femoral epiphysis scfe | Bilateral Slipped Capital Femoral Epiphysis | exact | y | 9 | 4 | 50307 | finish | finalize/0 |
| case-10 | spondylolisthesis with follow up | Lumbosacral transitional vertebra (Castellvi type IIIb) with severe lumbosacral kyphosis, status post posterior spinal fusion | wrong | n | 13 | 8 | 77446 | finish | finalize/0 |

## S4

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Appendiceal intussusception | exact | y | 9 | 6 | 99558 | finish | finalize/0 |
| case-02 | breast fibromatosis | Desmoid-type fibromatosis | exact | y | 14 | 6 | 57006 | finish | finalize/0 |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Thoracic spondylosis | compatible | n | 9 | 3 | 54754 | finish | finalize/0 |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic diverticulum | exact | y | 6 | 2 | 57781 | finish | finalize/0 |
| case-05 | extensive multi vessels coronary artery disease | Severe multivessel coronary artery disease | exact | y | 14 | 6 | 110626 | finish_after_revisit | revisit/1 |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 11 | 5 | 49800 | finish | finalize/0 |
| case-07 | patellar tendon rupture | Prepatellar Bursitis | wrong | y | 16 | 10 | 94198 | finish_after_revisit | revisit/2 |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Classic Bladder Exstrophy | wrong | y | 14 | 6 | 88523 | finish_after_revisit | revisit/2 |
| case-09 | slipped capital femoral epiphysis scfe | Slipped Capital Femoral Epiphysis | exact | y | 9 | 4 | 69904 | finish | finalize/0 |
| case-10 | spondylolisthesis with follow up | Grade III L5-S1 isthmic spondylolisthesis, status post L5-S1 fusion. | exact | y | 14 | 6 | 70700 | finish_after_revisit | revisit/1 |

## S5

| case | reference | diagnosis | grade | top3 | calls | imgs | tokens | stop | critic |
|---|---|---|---|---|---:|---:|---:|---|---|
| case-01 | appendiceal intussusception | Acute appendicitis | wrong | n | 12 | 22 | 119794 | finish_after_revisit | revisit/1 |
| case-02 | breast fibromatosis | Desmoid-type fibromatosis | exact | y | 12 | 7 | 96058 | finish | finalize/0 |
| case-03 | degenerative osteophyte mimicking metastasis false positive bone scan | Bladder cancer | wrong | n | 9 | 5 | 62937 | finish | finalize/0 |
| case-04 | epiphrenic esophageal diverticulum | Epiphrenic Diverticulum | exact | y | 5 | 2 | 31266 | finish | finalize/0 |
| case-05 | extensive multi vessels coronary artery disease | Coronary artery disease | exact | y | 12 | 12 | 144808 | finish_after_revisit | revisit/1 |
| case-06 | fibroadenoma | Fibroadenoma | exact | y | 9 | 5 | 47047 | finish | finalize/0 |
| case-07 | patellar tendon rupture | Patellar tendinopathy with partial tear | compatible | n | 12 | 14 | 129079 | finish | finalize/0 |
| case-08 | patent urachus with associated congenital urinary tract anomalies | Developmental dysplasia of the hip | wrong | n | 17 | 18 | 139737 | finish_after_revisit | revisit/2 |
| case-09 | slipped capital femoral epiphysis scfe | Slipped Capital Femoral Epiphysis | exact | y | 9 | 4 | 49051 | finish | finalize/0 |
| case-10 | spondylolisthesis with follow up | Isthmic spondylolisthesis at L5-S1 | exact | y | 13 | 8 | 84400 | finish | finalize/0 |
