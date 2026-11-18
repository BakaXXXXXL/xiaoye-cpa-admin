# CPA 管理操作手册

## 组成

- `catalog/providers/*.yaml`：人类和 Agent 共同维护的模型能力源文件，按渠道分组。
- `access.yaml`：下游用户、模型策略、接口权限和限流。
- `secrets.json`：明文 Key 与上游映射，仅服务器读取，权限必须为 `600`。
- `model-capabilities.json`：由模型目录生成，供现有下游同步接口使用。
- `cpa-admin`：唯一日常管理入口。

不要把实际 `secrets.json`、服务器 `access.yaml` 或线上模型目录提交到公开仓库。

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
