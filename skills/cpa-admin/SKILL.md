---
name: cpa-admin
description: Manage CPA model capabilities, downstream-key access and upstream model pools with cpa-admin. Use when listing, adding, disabling, granting or revoking models, changing key limits/endpoints, inspecting pool channels, testing or changing channels, validating, applying or rolling back CPA configuration.
---

# CPA 管理

通过 `cpa-admin` 管理模型目录和下游权限。不要直接编辑服务源码、Caddy、systemd、`config.json` 或 CPA 核心配置。

## 安全边界

- 不读取、复制或输出 `secrets.json`、私钥、管理 Key 或 `.env`。
- 查询、校验和 `diff` 可直接执行。
- 新增模型和修改权限只生成草稿；完成后展示 diff。
- `apply`、`rollback`、创建/轮换 Key、扩大模型范围、启用 credits 前必须获得用户明确确认。
- 创建或轮换 Key 时，新 Key 只显示一次；不要把 Key 写入文档、Git 或日志。
- 不用真实推理请求做日常验证；用户明确要求时才执行。

## 工作流

先检查：

```bash
cpa-admin validate
cpa-admin models list
cpa-admin access list
```

查看单项：

```bash
cpa-admin models show qwen-payg/Qwen4-Max
cpa-admin access show friend
```

新增模型草稿：

```bash
cpa-admin models add provider/model-id \
  --name "Display Name" \
  --input text,image \
  --context 1000000 \
  --output 65536 \
  --tools \
  --reasoning low,high,xhigh \
  --default high
```

非推理模型省略 `--reasoning` 和 `--default`。只有确认可显式关闭推理时才使用 `--supports-disable`。

权限草稿：

```bash
cpa-admin access grant friend qwen-payg/Qwen4-Max
cpa-admin access revoke friend mimo/MiMo-V2.6-Pro
cpa-admin access set-rpm friend 100
cpa-admin access endpoint friend credits off
cpa-admin access disable friend2
cpa-admin access enable friend2
```

发布前：

```bash
cpa-admin validate
cpa-admin diff
```

得到用户确认后：

```bash
cpa-admin apply
```

如果验证失败，`apply` 会尝试自动恢复。需要人工回滚时先取得确认，再执行：

```bash
cpa-admin history
cpa-admin rollback
```

创建 Key 是敏感操作。确认后执行 `cpa-admin access create-key NAME`，将一次性的新 Key 安全交给用户，不要保存到仓库。

## 聚合池

只读检查：

```bash
cpa-admin pool list
cpa-admin pool show xiaoye/auto
```

渠道修改只生成草稿：

```bash
cpa-admin pool disable xiaoye/auto channel-id
cpa-admin pool enable xiaoye/auto channel-id
cpa-admin pool set-priority xiaoye/auto channel-id 80
cpa-admin pool diff
```

渠道启停、池发布和回滚需要用户确认：

```bash
cpa-admin pool apply
cpa-admin pool rollback
```

只有用户明确要求测试真实渠道时才执行 `cpa-admin pool test POOL CHANNEL`，因为这会产生少量上游请求。优先级是 CPA provider 级设置；修改一个渠道可能更新同 provider 的其他渠道，必须向用户说明。

不要移除心跳、视觉预处理、UA 分流或 LongCat 思考转换。池管理只更新 CPA 核心的受管模型映射。

## 排错

- `unknown model`：先添加模型目录，再授权。
- `missing secret ref`：停止操作并告诉用户密钥引用不完整，不尝试查看密钥文件。
- 服务验证失败：报告错误并保持或恢复备份，不反复重启。
- 下游未看到更新：先运行 `cpa-sync sync <target> --verify`，不要修改服务端来掩盖客户端缓存问题。
