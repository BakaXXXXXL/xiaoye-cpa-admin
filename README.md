# xiaoye CPA Admin

面向人工与 Agent 协作的 CPA 模型目录和下游权限管理工具。

- 按渠道维护可读 YAML 模型目录。
- 统一管理每把下游 Key 的模型范围、接口权限、RPM 和上游映射。
- 同一策略驱动 `/models`、`/capabilities`、Chat、Responses、Keeper 和 Credits。
- 提供校验、差异预览、自动备份、发布验证和回滚。
- 附带 Hermes Skill 和中文操作手册。
- 实际密钥和线上策略不进入公开仓库。

## 文档

- [中文操作手册](docs/manual.zh.md)
- [Hermes Skill](skills/cpa-admin/SKILL.md)
- [权限配置示例](examples/access.yaml)
- [模型配置示例](examples/model.yaml)

## 常用命令

```bash
cpa-admin models list
cpa-admin access list
cpa-admin validate
cpa-admin diff
cpa-admin apply
```

草稿修改不会自动上线。`apply` 会生成能力 JSON、备份当前状态并验证服务；`rollback` 可恢复最近快照。

## 安全

`secrets.json` 必须仅存于服务器且权限为 `600`。Agent 不应读取密钥文件。创建/轮换 Key、扩大权限、发布和回滚需要人工确认。

本仓库是配套管理工具，不代替 CPA 核心，也不包含任何真实凭据。
