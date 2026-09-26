# 智能体名册 · agency-agents-plugin

> English: [README.en.md](README.en.md)

把 [agency-agents](https://github.com/msitarzewski/agency-agents) 的 279 位专家人格移植成本项目插件。
上游那套东西是给 Claude Code / Cursor / Codex 这类工具「拷文件」用的：`scripts/install.sh` 把 markdown
复制到 `~/.claude/agents/`，靠人在对话里喊「activate Frontend Developer mode」。本插件不拷文件——人格
留在 `catalog/` 里当**数据**用：可搜索、可整篇读出来当系统提示词、可直接向其提问、也可以被规划器当
子智能体路由。

零第三方依赖。上游文件一个字没改，MIT 许可证与来源提交号记在 [catalog/UPSTREAM.md](catalog/UPSTREAM.md)。

## 三十秒上手

```text
/agency-help                          先看一眼有什么
/agency-find react 性能                搜名册
/agency-brief frontend-developer      把这位的完整提示词读出来
/agency-ask frontend-developer LCP 4秒怎么查   让他用自己的口气回答
/agency-panel 给新手游做冷启动          组一个跨部门小组，并给出可直接用的节点配置
/agency 帮我把结账流程重做一版          交给规划器，由路由子智能体挑人
```

聊天里写「activate Frontend Developer mode，帮我看看这个组件」同样有效——`before_route` 钩子认识上游
这个习惯说法，会把它变成一条路由提示。

## 移植了什么，怎么映射

| 上游 | 这里 |
| --- | --- |
| `<division>/*.md` 人格文件 | `catalog/<division>/**/*.md`，原样保留（含 `game-development/unity/` 这类子目录） |
| `divisions.json` | 一起带过来，部门名称、图标色都从它读，不在代码里再抄一份 |
| `scripts/install.sh --tool claude-code` | 不需要：宿主直接从 `catalog/` 读，没有「装到哪里」这回事 |
| `scripts/convert.sh` + `integrations/` | 不需要：那是给别的工具的文件格式做转换 |
| README 里的「activate X mode」 | `before_route` 钩子识别，转成 `router_hints` |
| 「组一支梦之队」 | `agency_agents_panel_tool`，直接吐出 vote / dialogue / consensus 节点配置 |
| `strategy/`、`examples/` | 没带：没有 agent frontmatter，插件里没有任何东西能路由到它们 |

## 四个工具

| 工具 | 干什么 | 联网 | consequential |
| --- | --- | --- | --- |
| `agency_agents_roster_tool` | 按关键词/部门搜名册 | 否 | 否 |
| `agency_agents_brief_tool` | 读一位人格的完整提示词 | 否 | 否 |
| `agency_agents_panel_tool` | 组小组 + 给出节点配置 | 否 | 否 |
| `agency_agents_consult_tool` | 用该人格的身份回答一个问题 | **是** | **是** |

前三个是纯磁盘读取，规划器可以随便调；只有第四个花钱，所以只有它带 `consequential = True`，也只有它
声明了 `timeout_sec`（跟着 `llm.timeout_sec` 走，再加 5 秒余量，让请求先超时、返回干净的
`provider_error`，而不是被执行器当工具超时掐掉）。

四个工具都没有声明 `allowed_subagents`，清单里也没有 `tool_access`。这是故意的：有哪些专家子智能体是
**配置**决定的（`roster.subagent_divisions`），写死一份白名单会在有人启用新部门的当天失效，而且是静默
失效。收紧放在子智能体那一侧，每个子智能体自己声明用哪几个工具。

## 子智能体：一个路由 + 每个部门一个

名册有 279 位人格，**不会**注册 279 个子智能体——那等于把 279 份互相竞争的描述塞进规划器的候选提示词，
每一次路由都会更差。实际注册的是：

- `agency_agents_router`：domain 为 `agency-roster`。回答「这事该找谁」，以及点名了某位人格的请求。
- `agency_<部门>_specialist`：`roster.subagent_divisions` 里列出的每个部门一个，domain 就是部门名，
  capabilities 是该部门自己的人格列表（`backend-architect`、`sre`、`frontend-developer`……）——这正是
  任务描述长的样子。默认开 6 个：engineering、design、product、marketing、security、testing。

挑哪位人格是子智能体内部的一次名册查询，不是规划器的决定，也不花钱。

**匹配不到就不硬答。** 搜索是对一两句 frontmatter 描述做关键词匹配，名册里没人提过 Kubernetes，所以
「k8s 集群一直驱逐 pod」的最高分只有 3 分。这种情况下路由子智能体返回候选名单和部门列表
（`needs_selection: true`），而不是让一个财务追踪人格一脸认真地解释 pod 驱逐。部门专家不一样：部门是
规划器已经做出的决定，所以它一定在自己部门里挑一位回答，同时把 `low_confidence` 标出来。

所有子智能体都能当 `vote` 节点的投票者：收到 `_vote_spec` 时它会以人格身份表态，答案的**第一行**决定
approve / reject / abstain（会解释理由的人格正文里往往两个词都出现，所以只看开头）。没配模型时干脆
弃权并说明原因，而不是瞎猜。

## 配置

| 项 | 作用 |
| --- | --- |
| `llm.*` | 提问时用哪个 provider / model / 温度 / 请求超时 |
| `roster.subagent_divisions` | 哪些部门注册成子智能体。列表越长，规划器候选提示词越长 |
| `roster.max_results` | 搜索默认返回条数 |
| `roster.brief_max_chars` | 读人格提示词时截断到多长（返回值会说明是否截断） |
| `roster.persona_max_chars` | 咨询时**整份** system 提示词的预算，前缀与语言指令也算在内 |
| `consult.auto_run` | 子智能体挑好人格后是直接作答，还是只返回简报 |
| `consult.answer_language` | 上游人格全是英文写的；默认让模型跟着提问语言回答 |
| `consult.system_prefix` | 拼在人格前面的本部署边界，例如「不许编数据」 |
| `consult.keep_last` | 每个用户保留多少条咨询记录 |
| `panel.mode` / `panel.size` | `/agency-panel` 默认组什么样的小组 |

宿主的编译器对协作节点有硬约束：vote 至少 2 人最多 7 人、dialogue **恰好** 2 人、consensus 最多 7 位
判官。`panel` 工具按所选模式把人数夹紧，所以它给出的节点配置是能编译过的。

## 页面与面板

侧边栏「智能体名册」：搜索 → 选人 → 读提示词 → 直接提问 → 下载 markdown，中英双语、跟随明暗主题。
面板由 `plugin.yaml` 声明、宿主渲染，插件不写这部分前端：4 个指标卡、部门表、人格分布柱状图、
提问表单、最近咨询、最常请教的人、运行时状态、重新索引按钮。

## 名册是缓存的

建索引只读每个文件开头的 frontmatter，不读 4.5 MiB 的提示词正文——列表、搜索、面板都不需要正文，
只有 `brief` 和咨询才去读一整篇。索引在进程内缓存一次；换了磁盘上的文件之后，点面板上的「重新读取
名册」（`POST actions/refresh`）即可，不用重启。

## 更新名册

```bash
git clone --depth 1 https://github.com/msitarzewski/agency-agents.git /tmp/agency-agents
uv run python scripts/import_catalog.py /tmp/agency-agents --dry-run
uv run python scripts/import_catalog.py /tmp/agency-agents
uv run pytest
```

只动 `catalog/`，不动插件代码。人格数量在测试里钉住了，所以一次「导入完只剩一半」会在测试里失败，
而不是在线上。上游新增一个部门时，记得把它加进 `roster.subagent_divisions` 才会注册对应的专家。

## 开发

```bash
uv run python main.py inspect                            # 注册了什么
uv run python main.py doctor                             # 通用检查
uv run python main.py call agency_agents_roster_tool '{"query":"tiktok"}'
uv run python main.py call agency_agents_consult_tool '{"question":"LCP 4s","dry_run":true}'
uv run python main.py hook before_route '{"message":{"text":"/agency-help"}}'
uv run python main.py web GET agents '{"q":"shader"}'
uv run python main.py serve                              # 在浏览器里打开页面
```

在宿主仓库内：

```bash
uv run pytest plugins/agency-agents-plugin/tests -q
uv run ruff check plugins/agency-agents-plugin --no-respect-gitignore
uv run bandit -r plugins/agency-agents-plugin -c ../../pyproject.toml
```

`tests/test_plugin_contract.py` 与 `tests/harness/` 来自
[plugin-template](https://github.com/sxf51/plugin-template)，一行未改；
`tests/test_plugin_agency_agents.py` 是这个插件自己的行为测试。

## 许可

插件代码 MIT。`catalog/` 下的人格文件版权归上游作者，同为 MIT，许可证与来源提交号见
[catalog/UPSTREAM.md](catalog/UPSTREAM.md)。
