# CPA 管理操作手册

## 组成

- `catalog/providers/*.yaml`：人类和 Agent 共同维护的模型能力源文件，按渠道分组。
- `access.yaml`：下游用户、模型策略、接口权限和限流。
- `secrets.json`：明文 Key 与上游映射，仅服务器读取，权限必须为 `600`。
- `model-capabilities.json`：由模型目录生成，供现有下游同步接口使用。
- `cpa-admin`：唯一日常管理入口。

不要把实际 `secrets.json`、服务器 `access.yaml` 或线上模型目录提交到公开仓库。

## 只读 Web 仪表盘

通过服务器的 `/cpa-admin/` 路径访问。使用独立管理员凭据，不能使用下游模型 Key 代替。

仪表盘展示模型、权限、池、渠道和 systemd 服务状态。数据仅在打开页面或点击刷新时读取，不持续轮询服务器。页面不包含修改、发布或 Key 操作；这些仍按本文命令和审批流程执行。

## 日常查询

```bash
cpa-admin models list
cpa-admin models show qwen-payg/Qwen4-Max
cpa-admin access list
cpa-admin access show friend
cpa-admin validate
```

## 新增模型

```bash
cpa-admin models add qwen-payg/Qwen4-Max \
  --name "Qwen4 Max" \
  --input text,image,video \
  --context 1000000 \
  --output 65536 \
  --tools \
  --reasoning low,high,xhigh \
  --default high
```

参数说明：

- `--input`：模型真实输入模态。
- `--context`：上下文窗口。
- `--output`：最大输出 token。
- `--tools`：允许工具调用。
- `--reasoning`：支持的思考强度，逐模型填写。
- `--default`：默认强度，必须包含在 `--reasoning` 中。
- `--supports-disable`：只有确认能显式关闭推理时才添加。

停用模型：

```bash
cpa-admin models disable qwen-payg/Qwen4-Max
```

## 管理 Key 的模型范围

```bash
cpa-admin access grant friend qwen-payg/Qwen4-Max
cpa-admin access revoke friend mimo/MiMo-V2.6-Pro
cpa-admin access set-rpm friend 100
```

多个策略共用一个 principal 时，可用 `--policy POLICY_NAME` 指定策略。

接口权限：

```bash
cpa-admin access endpoint friend credits off
cpa-admin access endpoint friend credits on
```

可用接口为 `models`、`capabilities`、`chat`、`responses`、`usage`、`credits`。

停用或恢复整个 principal：

```bash
cpa-admin access disable friend2
cpa-admin access enable friend2
```

## 创建和轮换 Key

```bash
cpa-admin access create-key NAME
cpa-admin access rotate-key NAME
```

新 Key 只在终端显示一次。通过安全渠道交给用户，不要写入工单、聊天日志或 Git。创建和轮换后需要 apply 才会生效。

## 检查、发布和回滚

所有新增和修改先保存为草稿：

```bash
cpa-admin validate
cpa-admin diff
```

确认差异后发布：

```bash
cpa-admin apply
```

发布会备份当前策略、生成能力 JSON、重启相关管理服务并检查网关响应。失败时尝试恢复备份。

查看历史和回滚到最近快照：

```bash
cpa-admin history
cpa-admin rollback
```

回滚会覆盖当前草稿，因此执行前先保存仍需保留的变更。

## 聚合池与上游渠道

查看公开模型背后的渠道：

```bash
cpa-admin pool list
cpa-admin pool show xiaoye/auto
```

每个渠道有稳定的 `id`、CPA provider、上游原生模型名、启用状态和优先级。`config` 保留 CPA 的显示名称、模态等模型字段。

停用或恢复渠道：

```bash
cpa-admin pool disable xiaoye/auto CHANNEL_ID
cpa-admin pool enable xiaoye/auto CHANNEL_ID
```

调整优先级：

```bash
cpa-admin pool set-priority xiaoye/auto CHANNEL_ID 80
```

优先级实际属于 CPA provider，不是独立的单模型属性。修改时会同步更新同一 provider 的渠道引用，避免不同池之间产生冲突。

显式测试一条真实渠道：

```bash
cpa-admin pool test xiaoye/auto CHANNEL_ID
```

该命令会向上游发送一个最小聊天请求，会产生少量用量。429 表示上游限流，不能据此判定本地配置无效。

发布与回滚：

```bash
cpa-admin pool diff
cpa-admin pool apply
cpa-admin pool rollback
```

池发布会备份 CPA 核心配置和 pools.yaml，只替换受管理的模型映射，然后重启 CPA 核心并等待 `/models` 恢复。失败会恢复备份。心跳、视觉预处理、UA 分流和 LongCat 思考转换不会被移除。

## 下游同步

服务端变更生效后，下游运行：

```powershell
cpa-sync sync opencode --verify
cpa-sync sync dsh --profile dsh-tui --verify
```

DSH 的 profile 以 `.dsh/profiles` 目录名称为准。

## 人机协作建议

- 人可以直接编辑 YAML，也可以让 Agent 生成草稿。
- Agent 负责查询、修改、校验和解释 diff。
- 人负责批准权限扩大、Key 操作、正式发布与回滚。
- 每次发布只处理一组相关变更，方便审计和回滚。
