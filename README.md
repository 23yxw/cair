# 多模态诊断 Agent

由视觉语言模型（VLM）驱动的诊断 Agent。输入是一份入院文本和若干影像检查（CT、MRI、X 线、乳腺摄影、超声、透视、核医学、病理、照片、标注图），输出是一份结构化诊断报告，报告里的每条发现都要指回它所依据的那张图或那段文本。

## 一句话看懂

模型不是一次性看完所有材料就作答，而是像住院医生一样分步工作：先列出病例里有哪些检查，再决定看哪张图，把看到的写进一本**证据账本**，最后提交诊断。提交之后，另一次独立读图（**Critic**）只看它依据的几张原图并给出判断；两边对不上，就不交付具体病名，而是标为「未确认」。

## 它怎么工作

### 系统架构

一个 VLM 控制器在循环里调用工具；工具是文本和图像进入模型上下文的唯一通道；每条观察写进证据账本；`finish` 之后由代码审计和一次独立读图决定交付具体病名还是降级。

```
入院文本 + 检查目录
      │
      ▼
┌─ Agent 循环（每例最多 14 次计费调用、24 张图）
│
│   VLM 控制器 ──工具调用──▶ 工具注册表 ──▶ CaseRepository（文件 | 只读 SQLite）：病例文本、检查/序列目录、图像字节
│     ▲                       │ 绑定本次 case_id    ├──▶ 影像工具箱：anchors 代表切片 / uniform 联系表 / neighbors 邻层 / zoom / compare
│     │                       │ 计预算、校参数      └──▶ 证据账本：seen（展示过的 id + 分辨率 + 来源等级）、entries（claim + 引用 + 置信度）
│     │                       │                         门闩：direct 须引用全分辨率原图 · 不含第一诊断名 · 看图前 p ≤ 0.50
│     │                       ▼
│     └──── 工具返回的文本 + 带标签的图像 ◀───┘    图进了下一轮上下文才算「看过」；上下文只留 ≤4 张已看过的图
│
│   每一步 ──▶ 轨迹记录器 ──▶ runs/<时间>/<病例>.jsonl
└─ 循环直到 finish 被接受
      │ finish（候选诊断 + 鉴别诊断 + 关键发现）
      ▼
代码审计（账本结构）──▶ Critic：盲读 ≤4 张被 direct 引用的原图，不看账本、不看候选诊断
      │                         │
      │                         ├── 确认 ──────────────▶ Finalizer（诊断名固定，发现逐字抄账本）──▶ DiagnosisReport JSON
      │                         │
      └── 有硬问题 ──────────────┴── 不确认 / 沉默 ─────▶ 诊断 = undetermined，候选留在 uncertainty，degraded = true

模型拿不到：discussion.txt（真实标签）· 病例文件夹名 / 文件路径 · 其他病例的材料 · findings.md（默认隐藏）
```



### 一轮循环里发生什么

控制器（`cair_agent/agent/controller.py`）每一轮按下面的顺序走，直到 `finish` 被接受：

1. **整理上下文。** 已看过的图最多留 4 张，联系表先换成文字占位；刚取回、模型还没描述的一批不动。
2. **调一次模型。** 发系统提示、对话历史和工具 schema，输出额度 6000。预算用尽或连续 3 轮账本没有新内容时，只给 `finish` 一个工具。
3. **模型回复若干工具调用。** 一次回复可以带多个调用，预算按调用逐个计。
4. **代码先检查，再执行。** 计费调用是否还有额度；检查、序列、图像是否属于本次 `case_id`；取图的这一轮不允许 `record_evidence` 或 `finish`。通过了才执行工具。
5. **结果回到上下文。** 工具返回的文本进对话；取到的图合成一条消息，每张图前有 `[img:<id> | 检查 模态 | 序列说明 | 第 i/n 层]` 标签。图放进这条消息时，才把它的 id 标成「看过」。
6. **记轨迹。** 请求、回复、每个工具调用和返回、账本新增、假设更新，全部追加到 JSONL。
7. **回到 1。** `finish` 被接受的条件：引用的 id 都展示过，并且没有只靠联系表的 `direct` 结论还没补看邻层。



### 谁决定、谁强制


| 环节   | 模型自己决定                       | 代码强制                                              |
| ---- | ---------------------------- | ------------------------------------------------- |
| 看什么  | 哪个检查、哪个序列、哪种策略、哪一层           | 14 次计费调用、24 张图；只认本次 `case_id`；取图当轮不能记账            |
| 记什么  | 观察文字、`direct`/`indirect`、置信度 | 只认展示过的 id；`direct` 须引用全分辨率原图且不含第一诊断名；看图前概率 ≤ 0.50 |
| 何时停  | 何时调用 `finish`                | 只靠联系表的 `direct` 退回一次；预算尽或无进展时强制 `finish`          |
| 交付什么 | 候选诊断、鉴别诊断、关键发现               | 审计或盲读未确认则 `undetermined`；诊断名固定为提交原文；发现逐字抄账本       |




### 工具

病例文本和图像只能通过工具（`agent/tools.py`）进入上下文：

1. **建病例地图**：`get_case_profile`、`list_studies`、`list_series`。这三步不占调用预算。
2. **列假设**：`update_hypotheses`。在看到任何图像之前，任一假设的概率不得超过 0.50。入院文本只是转诊理由，不是诊断。
3. **取图**：`sample_images` 有三种策略。`anchors` 取代表切片；`uniform` 把多层拼成一张联系表做总览；`neighbors` 围绕可疑层逐张看。还有 `zoom_image` 放大、`compare_images` 并排。
4. **记账**：`record_evidence`。只有已经展示给模型的图像 id 才算「看过」；取图那一轮不能记账，图进了下一轮上下文才算。`direct`（亲眼所见）必须引用至少一张全分辨率的原始图像，联系表上的小图和标注图只能支撑 `indirect`（推断）。`direct` 的句子里不能出现当前第一诊断的名字。
5. **提交**：`finish`。若某条 `direct` 结论只靠联系表，并且还有预算，`finish` 会被退回一次并点名去看邻层。



### finish 之后

加上 `--critic` 后，提交的诊断要过两道检查。**代码审计**查账本结构：有没有只靠联系表的 `direct`、同一序列上前后矛盾的描述、来源和引用对不上。**Critic** 不看账本和候选诊断，只盲读 `direct` 结论引用的最多 4 张原图，先写画面上有什么，再在「提交的诊断」和「排名第二的假设」之间选一个。两道都通过，**Finalizer** 写出通过 schema 校验的报告，诊断名固定为模型提交的原文，关键发现逐字抄自账本。任何一道没过，报告诊断就是 `undetermined`，候选诊断只留在 uncertainty 里，并标 `degraded`。Critic 没能给出结论时也按未确认处理，不当成同意。

## 上下文管理

一个病例的 CT 可以有 679 层，模型的上下文装不下，也不该装下。这个项目里，**上下文是工作台，账本才是记忆**。

**每一轮进上下文的东西。** 系统提示和工具卡片；一句开场（「诊断 case-01」）；模型自己发出的工具调用；每个工具返回的文本（病例地图、序列清单、`slice -> image_id` 对照、剩余预算）；以及一条图像消息：每张图前面是标签 `[img:<id> | 检查 模态 | 序列说明 | 第 i/n 层]`，后面是一句「描述你看到的，然后记账」。标签让模型能用 id 引用这张图，而不是用文件名。

**什么时候裁掉。** 控制器里同时保留最多 4 张已经看过的图。超出时先把联系表换成一行文字占位「图像已展示过，你的观察在账本里」，再换最早的全分辨率图。刚取回、模型还没来得及描述的那一批不裁，哪怕它有 5 张邻层。文字轮次（记账、重试 `finish`）不会让图像变老。这是改过的：第一版按轮次老化，3 个文字轮次之后图就没了，10 例里有 4 例定稿时上下文里一张图都没有。

**为什么账本是记忆。** 图被裁掉后，模型对它的观察仍以文字留在账本里，并带着 id、序列、层号、`direct`/`indirect`、置信度。Finalizer 读的是账本摘要和候选诊断，不读对话历史。Critic 更极端：不读账本，只拿账本里 `direct` 结论引用的那几张原图重新看。账本因此有三个消费者（控制器自己、Finalizer、Critic 的选图），而上下文只有一个。

**什么永远不进上下文。** 文件路径（标签里只有 id，报错里的路径替换成 `[path]`）；`discussion.txt`（数据接口没有入口）；其他病例的检查、序列、图像（工具绑定本次 `case_id`）；默认设置下的放射报告。

**输出额度。** 控制器每轮 6000 token，思考开着。Critic 盲读 16000，正文为空时关掉思考再要一次 JSON；这是因为上一版 6000 的额度被 2 万字符的隐藏推理用光，正文为空被当成了同意。Finalizer 3000、关思考，只做格式。每次请求都把 `n_images_in_context` 写进轨迹，事后能查每一轮模型眼前有几张图。

**代价。** 同样 4 个病例，改造前约 41 万 token；第一版按张数保留图像后涨到 62 万，因为图留在后续轮次里反复计费；加上「刚取回的保留、看过的最多 4 张」之后回到 42 万。单轮基线平均 1.4 万，取证循环平均 8 万，差 5 倍。

## 安装

需要三样东西：Python 3.9 以上、一个 OpenAI 兼容的 VLM 端点（地址和 key）、病例数据包。数据包不在仓库里，由出题方单独提供。

```bash
# 1. 环境（任选其一）
python -m venv .venv && source .venv/bin/activate          # 标准 venv
# conda create -n cair python=3.11 -y && conda activate cair  # 或 conda

# 2. 安装（可编辑模式，含 pytest；装完后多出一个 cair-agent 命令）
pip install -e ".[dev]"

# 3. 端点
cp .env.example .env
# 用编辑器打开 .env，填三项：OPENAI_BASE_URL、OPENAI_API_KEY、CAIR_MODEL
# CAIR_MODEL 填端点 GET /v1/models 返回的 id；当前面试端点是 Qwen3.6-35B-A3B

# 4. 数据
# 病例已经在仓库的 data_subset10/（里面直接是各病例文件夹）
# 换一份数据也可以：在 .env 里把 CAIR_DATA_DIR 指向那个目录
```

安装是否成功，跑一次测试即可（不需要端点，不消耗 token）：

```bash
pytest -q        # 45 passed
```

`.env` 和 `runs/` 在 `.gitignore` 里，不会被提交。`data_subset10/` 随仓库提供。

## 运行

病例索引和 SQLite 库会在第一次运行时自动生成到 `runs/`，不需要先手动建。下面按「先确认端点，再跑一例，再跑全部」的顺序。

**第一步：确认端点能用**（约 1 分钟，四次小请求）

```bash
python scripts/smoke_vlm.py
```

依次测文本、图像、工具调用、多轮工具往返。最后一行应是 `TRANSPORT DECISION: native`。缺 `.env` 或找不到数据会直接报错退出。

**第二步：跑一个病例**（约 2 到 3 分钟，10 万 token 上下）

```bash
cair-agent run -c case-01 --critic
```

结束时终端打印一行 JSON：诊断、停止原因、工具调用数、图像数、token、耗时、是否降级。产物在 `runs/<时间戳>/`：


| 文件                    | 内容                                            |
| --------------------- | --------------------------------------------- |
| `case-01.jsonl`       | 完整轨迹：每次模型请求、每个工具调用及返回、每条账本、Critic 的盲读和选择、降级事件 |
| `case-01.report.json` | 最终报告：诊断、鉴别诊断、关键发现（带图像 id）、不确定性、下一步检查          |
| `summary.json`        | 本次所有病例的一行汇总                                   |


轨迹看起来不方便，可以渲染成 Markdown：

```bash
cair-agent render-trace runs/<时间戳>/case-01.jsonl --out trace.md
```

**第三步：跑全部 10 例**（约 25 分钟，100 万 token 上下）

```bash
cair-agent run -c all --critic
```

**常用开关**


| 开关                                    | 作用                            | 默认           |
| ------------------------------------- | ----------------------------- | ------------ |
| `--critic`                            | 打开代码审计 + Critic 盲读 + 降级       | 关            |
| `-c case-01 -c case-03`               | 指定多个病例；`-c all` 跑全部           | 必填           |
| `--backend sqlite`                    | 从只读 SQLite 取数据，而不是文件；库不存在会自动建 | `file`       |
| `--findings visible`                  | 让模型读放射报告；多份报告直接写出诊断，只用于上界对照   | `hidden`     |
| `--out runs/my-run`                   | 指定输出目录                        | `runs/<时间戳>` |
| `--max-tool-calls 10 --max-images 16` | 临时改预算                         | 14 / 24      |


**评测矩阵**（S1–S5 全跑约 2 小时，评分还会额外调用模型）

```bash
cair-agent eval --settings S3                 # 只跑主线
cair-agent eval --settings S1,S2,S3,S4,S5     # 完整矩阵 -> runs/eval-<时间戳>/summary.md
```

评分模型读参考诊断和 `discussion.txt`，诊断过程读不到。各设置的含义见后文「结果与交付物」。

`python -m cair_agent.cli ...` 和 `cair-agent ...` 等价。预算写在 `.env`：`CAIR_MAX_TOOL_CALLS`（默认 14；`get_case_profile`、`list_studies`、`list_series`、`record_evidence`、`update_hypotheses`、`finish` 不计入）、`CAIR_MAX_IMAGES`（24），以及单次调用和单病例超时。

`case-01` 到 `case-10` 是数据目录按文件夹名排序后的匿名编号，不是诊断名称。

## 数据暴露策略（Agent 能看什么、不能看什么）


| 材料                                    | Agent 能否访问                                 | 说明                                        |
| ------------------------------------- | ------------------------------------------ | ----------------------------------------- |
| `patient_data.txt`、`presentation.txt` | 能，经 `get_case_profile`                     | 空字段会标成 `missing`                          |
| 检查 / 序列结构（`series.json`）              | 能，经 `list_studies` / `list_series`         | 模态、说明、张数、代表切片                             |
| 图像                                    | 能，且只能经影像工具                                 | 以 `img:<image_id>` 引用；**不向模型展示文件路径**      |
| `findings.md`（放射报告）                   | **可配置**：`--findings hidden`（默认）或 `visible` | 主评测隐藏，因为多份报告直接写出诊断                        |
| `discussion.txt`（真实标签）                | **不能**                                     | `CaseRepository` 没有入口；只有 `eval/` 读取       |
| 病例文件夹名（即诊断标签）                         | **不能**                                     | 重命名为 `case-01..10`；轨迹会做泄漏检查               |
| 其他病例的材料                               | **不能**                                     | 每次运行的工具都绑定本次 `case_id`，跨病例的检查、序列、图像请求会被拒绝 |
| `image_urls.txt`                      | 不使用                                        | 离线运行；图像以 base64 发送                        |


端点地址和 API key 只从环境变量读取。`.env` 被 git 忽略，也不会出现在轨迹里。工具报错里的文件路径会被替换成 `[path]`，轨迹里也是。

## SQL 后端

`SqlCaseRepository`（`cair_agent/data/sql_repository.py`）用 SQLAlchemy Core 实现与文件后端相同的接口：固定的参数化查询模板，每条查询都有 `LIMIT`，连接只读（SQLite 用 `mode=ro`；Postgres/MySQL 用只读账号，经 `SqlCaseRepository.from_url`）。每次查询记入审计列表，控制器把它写成轨迹里的 `sql_query` 事件。`tests/test_sql_backend.py` 断言两个后端返回的病例事实逐字段相同，并且写入会被拒绝。

## 目录

```
cair_agent/
  config.py              环境变量与预算
  data/                  index.py（扫描并匿名化）、models.py、repository.py（文件）、sql_repository.py、schema.sql、build_sqlite.py
  imaging/               sampler.py（anchors/uniform/neighbors）、ops.py（缩放/裁剪/联系表）、encode.py
  llm/                   client.py（openai 封装、重试、用量）、schemas.py（pydantic 契约）
  agent/                 tools.py（注册表与 expert card）、ledger.py、controller.py、critic.py、finalizer.py、prompts/*.md（带版本）
  trace/                 recorder.py（JSONL）、render.py（Markdown）
  eval/                  single_pass.py（S1）、run_matrix.py（S1–S5）、judge.py、metrics.py
  cli.py                 run | eval | render-trace | index | build-db（后两个会在首次运行时自动执行）
tests/                   pytest（索引、影像、账本门闩、Critic 决策、上下文修剪、降级、SQL 一致性；45 项）
scripts/smoke_vlm.py     端点冒烟测试
docs/                    example_trace.md
```



## 结果与交付物

- `docs/example_trace.md`：完整渲染轨迹（case-07，S3：X 线 + 5 个 MRI 序列，含 Critic 与 Finalizer）
- `docs/results/eval_summary.md` 与 `S*_metrics.json`：S1–S5 矩阵（每种设置 10 个病例，严格 LLM 评分）
- `docs/results/traces/`：全部 S3 的 JSONL 轨迹和最终报告
- `runs/withhold-check/`：改造后 4 例badcase（case-01、03、08、10）的轨迹和报告

