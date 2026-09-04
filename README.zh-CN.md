# AxiomRelay

[English](README.md) | 简体中文

**A proof hill-climbing system for hard mathematical problems.**

保留已经核过的部分，一次只修一个 gap。

AxiomRelay 用来编排长篇数学证明，重点是失败恢复和结果审计。

它把路线设计、证明搜索、综合、验证和发布拆开执行。模型写出的内容一律先当草稿。发布权只在主控程序（host）手里。发布前，必须由两轮互相独立的整篇验证对完全相同的证明字节都给出 `correct`。

> **当前状态：** 这是研究型基础设施。流程、来源和重放记录都有严格约束，但模型验证仍可能出错，不能代替专家审稿。

## 为什么需要 AxiomRelay

普通的聊天循环很难稳妥处理高难度证明。几条看似不同的路线，可能其实来自同一个想法。对话拉长以后，早期前提也容易悄悄漂移。外部建议可能被误当成已经证明的结论，失败重试还会重复触发昂贵的模型调用。一份接近正确的草稿，也很容易被当成已经验证过的定理。

AxiomRelay 为这些问题加了几条硬约束：

- 设计路线和实际写证明由不同角色完成。
- Host 检查计划后，最多放行三条相互隔离的证明分支（lane）。
- 可选的 Opus + Sol council 会先各自设计路线，随后共同修订一次，最后只读审查风险。
- 用户提供的完整外部答案以 SHA 绑定的**未验证参考候选**保存。
- 后期从 GPT Pro 获得的局部帮助以**未验证 gap delta**保存，只绑定到指定缺口。
- 持久化意图（durable intent）和回执（receipt）用于恢复中断任务，避免重复同一次付费调用。
- 最终证明必须通过两个冷启动、按内容寻址的验证轮次（pass）。

## 主要思路：保留已核过的结果，只修当前缺口

AxiomRelay 针对这样一类题：整篇证明很难一次完成，但失败位置还能找出来，局部论证也能单独核验。

每轮不必推倒重写。系统会留下已经核过、彼此兼容的部分，把当前失败收缩成一个真正卡住证明的缺口（gap），然后只为这个 gap 调用额外模型或人工顾问。这种做法可以叫 hill climbing，含义很朴素：每次推进一小段，同时保留此前通过审计的结果。

```text
多路证明搜索
      |
      v
保留兼容且已经核过的部分
      |
      v
从两次独立失败中定位关键 gap
      |
      v
owner 把这个具体问题交给 GPT Pro（可选，带 SHA 绑定）
      |
      v
未验证 gap delta -> 局部审计 -> 只重做受影响的部分
      |                                   |
      +------------- 发现新 gap <---------+
                           |
                           v
                       恢复多路搜索
```

GPT Pro 只在少数关键 gap 上提供建议，相当于一个低频调用的 **gap oracle**（缺口顾问）。它的输出仍是参考，不带证明权威。其余角色各管一段：

- root 保存当前状态，决定下一步处理哪个瓶颈。
- 相互隔离的 Sol lanes 负责尝试不同方向。
- Pro 可以给出关键的局部论证或反例，但内容仍需审计。
- verifier 最后检查的是完整证明。

Pro 的回答即使不成立，也不会抹掉此前的进度。若其中某一步通过审计，就把它并入现有证明。下一次提问会重新绑定题目、引用记录、失败证据和最新的 ledger head，不依赖一段越来越长、难以追踪的聊天历史。

这套方法适合可分解的中高难度题目：一次成稿的成功率不高，但引理、估计、奇异区域、边界情形或反例仍能分开检查。简单题没必要承担这些开销。真正的开放问题，或者无法可靠检查局部 gap 的题目，也不一定适用。

## 系统流程

```text
题目陈述 ------------------------------+
                                        |
可选 GPT Pro / 人工完整答案 -----------+ 作为未验证、SHA 绑定的输入保存
                                        v
                                  路线设计 root
                 GPT Sol | Opus | Fable | Opus + Sol council
                                        |
                               一份获准执行的三路线计划
                                        v
                            三条隔离的 GPT Sol 证明 lane
                              |         |         |
                              +---------+---------+
                                        v
                                root 综合并自查
                                        v
                               blueprint.md（草稿）
                                        |
                           冷启动 verifier pass 1：是否正确？
                                        | 是
                           冷启动 verifier pass 2：是否正确？
                                        | 是
                                        v
                                原子发布 + 不可变 receipt
```

Host 负责准入、fencing（隔离并作废旧实例）、恢复和发布。root 设计路线并综合证明。证明 lane 不能继续派生 agent，verifier 也没有编辑或发布证明的权限。

### AxiomGraph 桥接（实验性基础）

当 `rethlas-publication-v6` 已经通过原有权威流程完成 reconcile 后，host 会通过一个
版本化、仅使用标准库的 wire interface，以 best-effort 方式写出不可变 source event。
规范 manifest 固定在 `agents/generation/mcp/axiomgraph_source_interface_v1.json`。
每个 event 都绑定题目和 blueprint 的精确字节、publication receipt、规范化 ProofItem
DAG、稳定 verifier profile，以及实际加载的 Core/export runtime digest；event 按
publication receipt 与 event id 保存在
`agents/.claude_core/axiomgraph_exports/v1/publications`。

AxiomRelay 不再 import AxiomGraph，也不在内部构造 AxiomGraph 对象。独立版本的 consumer
读取这套 source protocol，并且只有在核对 interface major/minor、required capability、
精确 AxiomGraph schema digest 和 runtime source binding 后才能转换 event。因此，Relay
内部重构只要继续保持 v1 语义就能兼容；破坏语义的修改必须发布新的 interface major 与
event schema。export 失败不会改变原 publication 状态、字节、receipt 或 API 返回值，
同时会留下有界的本地失败审计。

这还不是 `stopped_unsolved` 的自动接管触发器。只有当 AxiomRelay 能认证同一个终局
cohort、source state、没有未完成的 owner/Pro wait，并完成 lease/fence CAS 后，才会
开放向 Danus controller 的自动转移。一次有界的 `stop_unsolved` 只说明 fast path
停止了，绝不能被改写成“已经证明该定理在数学上不可能”。

## 什么才算成功

一次运行只有满足下面五项条件才算成功：

1. `blueprint.md` 能解析成完整的 proof-item manifest。
2. Verifier Pass 1 对每个必需 item 都返回 `correct`。
3. Verifier Pass 2 独立检查同一份不可变证明，也对每个 item 返回 `correct`。
4. receipt 中记录的题目、证明、上下文、模型、effort、pass 身份和服务 digest 全部吻合。
5. Host 以原子操作写入 `blueprint_verified.md` 和相应的 publication receipt。

路线报告、综合报告、草稿或单次 verifier pass 都不能单独代表成功。即使存在 `blueprint_verified.md`，缺少 receipt 也不算正式发布。

## 执行选项

### 模式

| 模式 | 适用场景 | 可选 root | 调用成本 |
|---|---|---|---|
| `core` | 默认隔离运行时 | GPT Sol、Opus、Fable、Opus + Sol council | 较低开销 |
| `reviewed` | 保留定时 review 的长期兼容流程 | GPT Sol | 较高开销 |

非交互运行默认选择 `core`。交互运行会列出各选项并给出说明。使用 `reviewed` 时必须显式提供 run ID。

### Root

| Root | 角色 |
|---|---|
| `gpt-sol` | 默认选项。由 GPT Sol 设计路线并编排证明工作。 |
| `opus` | 逻辑身份可持续的 Claude Opus 5 root。每次启动推进一个可恢复的 turn。 |
| `fable` | 逻辑身份可持续的 Claude Fable 5 root，受与 `opus` 相同的 host 管控。 |
| `opus-sol-council` | Opus 和隔离的 Sol/max seat 分别设计路线，共同修订一次，最后做只读审计。 |

Claude root 目前只能用于 `core` 模式。它只能查看只读 workspace，并通过少量 host 接口执行规定动作。通用 shell、文件写入、浏览器和 subagent 权限均未开放。

### 模型策略配置

| Profile | 证明 lanes | Verifier 1 | Verifier 2 |
|---|---|---|---|
| `compatible` | Sol `max` | Sol `xhigh` | Sol `xhigh` |
| `balanced` | Sol `max` | Sol `xhigh` | Terra `max` |
| `economy` | Terra `max` | Sol `xhigh` | Terra `max` |
| `max_diversity` | GPT-6 Astra `max` | GPT-6 Astra `max` | Opus 5 1M `max` |

`max_diversity` 要求 OpenAI 和 Claude 两个 provider 都已完成认证。Pass 2 会冷启动，只接收经过认证的证明上下文，不会看到 Pass 1 的判定、findings 或 session state。

该配置下 council 的 OpenAI 席位也使用 `gpt-6-astra`、推理强度 `max`。
`gpt-sol` 和 `opus-sol-council` 入口名称保持兼容。已有 session 保留原有的
源码及模型绑定，升级后需要走现有的 source-drift 接管流程；本次尚未执行
Astra 的付费端到端 canary。

## 快速开始

AxiomRelay 支持 Linux 和 macOS。Python 版本必须是 3.11、3.12 或 3.13，
Bash 则需要 5.0 以上。macOS 自带的 Bash 太旧，先安装新版并把它放到
`PATH` 前面：

```bash
brew install bash uv
export PATH="$(brew --prefix)/bin:$PATH"
```

Linux 上的外部 proof lane 使用无特权的 mount/PID namespace；macOS 上改用
Codex 自带的 Seatbelt permission profile。两种平台都会在第一次付费调用前
做一次不调用模型的隔离测试。

### 1. 克隆仓库并安装 CLI

```bash
git clone https://github.com/ot-triton-lab/AxiomRelay.git
cd AxiomRelay
npm install -g @openai/codex
```

开始运行题目之前，先完成 Codex 认证。如果要使用 Claude root 或 `max_diversity` 验证，还需安装并登录 Claude Code。
Claude 不限定某一家 provider：默认的 `auto` 跟随当前 CLI 登录状态。如果一台
机器同时配置了 Vertex、Bedrock、Foundry 或 API key，但某次 root 必须使用
Claude 订阅 OAuth，可显式设置 `AXIOM_RELAY_CLAUDE_AUTH_MODE=subscription`。
Verifier 对应的变量是 `VERIFY_CLAUDE_AUTH_MODE`。显式选择模式后，启动器会同时
核对 provider 和 Claude CLI 报告的认证方式；`subscription` 还要求 CLI 返回订阅
类型。任何一项对不上，都会在调用模型之前停止。
官方原生 Claude Code 安装会让版本目录里的可执行文件与 `ClaudeCode.app` 里的
可执行文件成为同一 inode 的两个硬链接，AxiomRelay 会识别这种固定布局。除此
之外，带额外硬链接的可执行文件仍会被拒绝。

### 2. 创建 Python 环境

为 verifier 创建环境：

```bash
python3 -m venv agents/verification/.venv
agents/verification/.venv/bin/pip install \
  -r agents/verification/requirements.txt
```

为生成端创建环境：

```bash
python3 -m venv --copies --without-pip agents/.generation-venv
uv pip install --python agents/.generation-venv/bin/python \
  -r agents/generation/requirements-dev.txt
```

生成端故意使用复制出来的 Python 解释器。Runner 会核验解释器及可信源码闭包。如果发现可执行的 `.pth` hook，会在触发付费调用前直接拒绝运行。

### 3. 启动 verifier

```bash
cd agents/verification
./scripts/run_verifier.sh
```

如需跨 provider 验证：

```bash
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
./scripts/run_verifier.sh
```

只打印最终命令、不启动服务：

```bash
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
AXIOM_RELAY_VERIFIER_PRINT_COMMAND=1 \
./scripts/run_verifier.sh
```

服务默认通过 HTTP 监听本机 loopback 的 `8091` 端口。正式跑题前应检查
`/ready`，不能只看进程是否还活着：

```bash
curl -fsS http://127.0.0.1:8091/ready
```

这个检查不会调用模型。它会核对 CLI 和认证状态、MCP/runtime import、持久化
目录、当前平台需要的系统能力，并实际跑一次 Codex sandbox probe。

远程部署时，必须先在反向代理等上游终止 HTTPS，并使用至少 256 bit 的随机
token；本服务本身不负责 TLS：

```bash
export VERIFY_API_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
export VERIFY_TLS_TERMINATED=1
```

### 4. 运行题目

先在本地把 UTF-8 Markdown 格式的题目放进 `agents/generation/data/`。这个目录里的题目和答案默认不会提交到 Git。目录层级会保留在结果路径中，例如 `data/algebra/problem.md` 的输出位于 `results/algebra/problem/`。

使用默认的 GPT Sol root：

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=gpt-sol \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

使用 `max_diversity` 配置的 Opus + Sol council：

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=opus-sol-council \
AXIOM_RELAY_MODEL_POLICY_PROFILE=max_diversity \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

如果不设置这些环境变量，直接在终端运行 `./tests/run_example.sh`，脚本会先让你输入题目路径，再打开运行模式选择器。非交互运行必须设置 `PROBLEM_FILE`，仓库不再假定存在示例题目。没有手动指定 Python 时，GPT-Sol 和 reviewed 模式会自动使用文档中创建的 `agents/.generation-venv/bin/python`。

## 证明卡住时，向 GPT Pro 追问一个 gap

这就是前面所说的 hill climbing 如何落地。需要人工介入时，优先转交一个明确的 gap，而不是把整篇证明重新交给 Pro。

当同一个关键结论分别被两种方法卡住后，canonical Claude root 会准备三样东西：

- 只能写入一次的 `gap_id`
- 可直接复制的精确问题 `copy_paste_prompt`
- 由 host 计算的 digest，用来绑定当前 statement、引用中的有效 memory records 和 ledger head

不必等三条全局路线全部失败才提出局部问题。生成查询包不会打开 ChatGPT，也不会消耗 Pro 对话次数，真正发送问题的人只能是 owner。Reviewed/GPT-Sol lanes 无权调用这四个 root 工具，只负责把 gap 和失败证据交回 canonical root。

复制给 Pro 的 prompt 必须完全自包含。默认 Pro 无法访问仓库、AxiomRelay memory、record id、hash、本机文件或之前的对话。因此，回答该 gap 所需的定义、假设、已确定事实、失败机制和边界条件都必须直接写成数学内容。用于溯源的 id 和 digest 只保留在私有 packet 与 receipt 中，不得写入外部 prompt。

`两个独立失败机制 → gap query → owner 转交 → 未验证 gap delta → repair-cone 审计`

通常由 root 调用 `prepare_pro_gap_query`。需要恢复状态或测试接口时，也可以使用下面的 owner CLI：

```bash
PROBLEM_PATH=agents/generation/data/my_problem.md
PROBLEM_ID=my_problem
STATEMENT_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$PROBLEM_PATH")"

agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --prepare-pro-gap-query \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent <<'JSON'
{
  "target_claim": "证明一致加权 resolvent 估计。",
  "settled_facts": ["径向边缘分布与精确平稳方程已经建立。"],
  "verified_fact_or_proof_ids": ["<活动 proof_steps record id>"],
  "failed_attempts": [
    "逐 fiber 反演无法控制径向导数。",
    "dyadic 局部化无法控制界面通量。"
  ],
  "failed_path_record_ids": [
    "<第一条活动 failed_paths record id>",
    "<第二条活动 failed_paths record id>"
  ],
  "boundary_checks": ["处理 I 与 gamma 同阶的区域，不得假设一致角向 gap。"],
  "recommended_exact_question": "<要交给 GPT Pro 的自包含数学问题，写全所有必需定义和假设>"
}
JSON
```

不要自行提交 `source_context_sha256`。Host 会逐条解析引用记录，拒绝已失效的记录和 channel 不匹配的记录，保存当时的 ledger head，再计算 digest。最终生成的问题会带上 settled facts、两条失败路径和全部 boundary checks，但内部 id 与 hash 只保留在 packet 中。发送给 Pro 时，只复制 receipt 返回的 `copy_paste_prompt`，不要附加 receipt metadata，也不能假设 Pro 会自行补回省略的上下文。

历史 v2 query 仍可用于审计，也可以绑定已经发送出去的回答，但系统不会再把旧版外部 prompt 返回用于新 relay。此时会返回 `external_relay_status=legacy_prompt_requires_new_gap_id`。要取得自包含 prompt，必须用新的 `gap_id` 创建 v3 query。

每次读取都会做 compare-and-swap 检查。恢复 query 时，必须带上创建 receipt 返回的 digest：

```bash
agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --get-pro-gap-query \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent \
  "$QUERY_SHA256"
```

收到 Pro 的回答后，可以直接粘贴到当前 owner turn，也可以把已保存的答案绑定到这次 query：

```bash
agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --ingest-pro-gap-response \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent \
  "$QUERY_SHA256" < pro-answer.md
```

Ingest receipt 会返回 `RESPONSE_SHA256`。此后读取答案时，两个 digest 缺一不可：

```bash
agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --get-pro-gap-response \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gap_uniform_resolvent \
  "$QUERY_SHA256" "$RESPONSE_SHA256"
```

回答会以 `complete_unverified_gap_delta` 保存。它不会改动已经接受的 route-council candidate packet，因此迟到的回答不会强制重开 council。Root 根据记录中的失败路径审计回答，只改指定 claim 及其下游依赖，再重验这部分 repair cone。不受影响的现有验证结果仍可复用。无论内容多好，Pro 的回答本身都没有验证权或发布权。

绑定 response 之前，query 状态是 `waiting_owner_pro_response`。绑定完成后，状态变为 `response_available`。每个 statement 最多保存 16 个 queries 和 16 个 responses，总字节数也有限制。问题内容或证据集一旦变化，就要换一个新的 `gap_id`。

## 搜索开始前导入完整的外部候选

如果手上已有一份完整的替代证明，应在 route acceptance **之前**导入：不能等 fan-out 开始后再加，也不能直接塞进 verifier prompt。后期的定点修补请走上面的 gap-delta 通道。完整文本导入时仍按不可信候选处理：

```bash
PROBLEM_PATH=agents/generation/data/my_problem.md
PROBLEM_ID=my_problem
STATEMENT_SHA256="$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$PROBLEM_PATH")"

agents/.generation-venv/bin/python -I -B agents/claude_core.py \
  --ingest-reference-candidate \
  "$PROBLEM_ID" "$STATEMENT_SHA256" gpt_pro < pro-answer.md
```

导入后，为该 statement 启动一轮新的 Opus + Sol council。外部候选会按下面的规则流转：

1. Host 保存原始内容，并把不可变字节绑定到 problem 和 statement digest。
2. Opus 必须把 candidate marker 和准确的 projection path 绑定到其中一条路线。
3. Sol 最初提交的 blind slate 与外部候选相互独立。
4. 联合 revision 和 final audit 会看到完整候选，必须实际检验它，或者指出致命缺陷。
5. 被绑定的 proof lane 只能读取指定 projection。
6. Verifier 仍只判断最终证明。外部候选本身不具备发布权。

修改 statement 后 digest 会随之改变，旧候选无法被误用。如果候选在 council 已经接受路线后才加入，就要重新开一轮 council。

## 输出与恢复

下面以 `PROBLEM_FILE=data/my_problem.md` 为例：

| 路径 | 含义 |
|---|---|
| `agents/generation/results/my_problem/blueprint.md` | 当前草稿，尚未验证 |
| `agents/generation/results/my_problem/blueprint_verified.md` | 正式发布的证明内容 |
| `agents/.verification_receipts/` | 可信的 verification 和 publication receipts |
| `agents/generation/memory/my_problem/` | 持久保存的 canonical research memory |

Verifier 中断后会从第一个尚未判定的 item 继续，已经完成且仍兼容的 item 不会重跑。若无法判断某项调用到底是否执行过，系统会按失败处理（fail closed）。

恢复持久 Claude root：

```bash
cd agents/generation
AXIOM_RELAY_RUN_MODE=core \
AXIOM_RELAY_MAIN_AGENT=opus \
AXIOM_RELAY_CLAUDE_SESSION_ID=<lowercase-uuid> \
PROBLEM_FILE=data/my_problem.md \
./tests/run_example.sh
```

可以通过 `AXIOM_RELAY_CLAUDE_OWNER_PROMPT`，在 resumed turn 或显式 takeover 时给 root 补充策略。它只代表操作方指令（operator direction），不能当作数学前提，也不带发布权限。新建 root 如果没有 resume/takeover 绑定，会拒绝这个设置。

## 配置

| 设置 | 含义 |
|---|---|
| `AXIOM_RELAY_RUN_MODE` | `core`、`reviewed` 或 `prompt` |
| `AXIOM_RELAY_MAIN_AGENT` | `gpt-sol`、`opus`、`fable`、`opus-sol-council` 或 `prompt` |
| `AXIOM_RELAY_MODEL_POLICY_PROFILE` | `compatible`、`balanced`、`economy` 或 `max_diversity` |
| `AXIOM_RELAY_REVIEW_RUN_ID` | `reviewed` 模式必填的运行 ID |
| `AXIOM_RELAY_CLAUDE_SESSION_ID` | 要恢复的 Claude root session |
| `AXIOM_RELAY_CLAUDE_TAKEOVER_FROM` | 显式 fence 并接管一个可恢复的 Claude root |
| `AXIOM_RELAY_CLAUDE_OWNER_PROMPT` | 恢复或接管时发给 Claude root 的 operator message |
| `AXIOM_RELAY_CLAUDE_CONTEXT_WINDOW` | Claude 上下文窗口，Opus 默认 1M |
| `AXIOM_RELAY_CLAUDE_AUTH_MODE` | Claude root 的认证方式：`auto`、`subscription`、`api`、`vertex`、`bedrock` 或 `foundry` |
| `AXIOM_RELAY_PRINT_COMMAND` | 只打印 Claude root 启动命令，不执行 |
| `PROBLEM_FILE` | `agents/generation/data/` 下的安全 Markdown 路径 |
| `CLAUDE_CONFIG_DIR` | Claude root 使用的可选配置目录 |
| `VERIFY_READY_URL`、`VERIFY_PROOF_URL` | Verifier readiness 和 proof 地址 |
| `VERIFY_CLAUDE_AUTH_MODE` | Verifier 使用的 Claude 认证方式；选项与上面相同 |
| `VERIFY_API_TOKEN` | 非 loopback 验证必须提供的 bearer token |
| `VERIFY_TLS_TERMINATED` | 非 loopback verifier 位于可信 TLS 终止层之后时必须设为 `1` |

旧的环境变量名、schema 标识和 receipts 会在一个过渡版本内继续保持可读取、可解析。新脚本应使用上表中的名称。历史 receipt 必须保留原始字节，不能只为改品牌名而重写。

## 信任边界

- 题目、证明文本、标签、注释和 reference candidates 一律按不可信数据处理。
- Proof lane 看不到 root transcript，也与其他 lanes 隔离。
- 每个 verifier item 都在新建的最小 workspace 中运行。
- 原始模型 stream 以及模型自行写入的 result files 都没有证明权威。
- 确定性的 host 代码负责核对 content digests、item coverage、模型来源和 pass 身份，并执行原子发布。
- 只读 sandbox 不能提供完整的机密性隔离。处理敏感的对抗输入时，应使用专用 container 或 OS account。

运行时状态、凭据、运行结果、receipts、虚拟环境和 provider 配置均不会提交到 Git。

## 开发

运行 verifier 测试：

```bash
PYTHONDONTWRITEBYTECODE=1 \
agents/verification/.venv/bin/python -m pytest -q agents/verification/tests
```

运行 generation 和 launcher 测试：

```bash
PYTHONDONTWRITEBYTECODE=1 \
agents/.generation-venv/bin/python -m pytest -q agents/generation/tests
```

修改共享 MCP 或 proof-context 代码后，需要重新生成并检查 legacy server：

```bash
agents/.generation-venv/bin/python -B \
  agents/generation/mcp/build_legacy_server.py --write
agents/.generation-venv/bin/python -B \
  agents/generation/mcp/build_legacy_server.py --check
```

这些测试不会触发付费模型调用。

## 仓库结构

| 路径 | 用途 |
|---|---|
| `agents/generation/` | 题目、root contracts、编排逻辑、MCP、运行脚本和测试 |
| `agents/verification/` | Verifier API、schemas、supervision、启动脚本和测试 |
| `agents/claude_core.py` | 持久 Claude root 的 host，以及 route-council 控制平面 |
| `agents/model_policy.py` | 不调用模型的角色和 profile 解析器 |
| `agents/MODEL_POLICY.md` | 模型角色和兼容性约束的详细说明 |
| `agents/generation/site/` | 可选的 Zola 结果浏览器，只负责展示 |

## 许可证

[Apache License 2.0](LICENSE)
