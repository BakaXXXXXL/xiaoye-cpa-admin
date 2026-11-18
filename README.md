# xiaoye CPA Admin

面向人工与 Agent 协作的 CPA 模型目录和下游权限管理工具。

- 按渠道维护可读 YAML 模型目录。
- 统一管理每把下游 Key 的模型范围、接口权限、RPM 和上游映射。
- 同一策略驱动 `/models`、`/capabilities`、Chat、Responses、Keeper 和 Credits。
- 创建聚合池草稿，增删、启停渠道，调整 provider 级优先级并测试指定上游。
- 提供校验、差异预览、自动备份、发布验证和回滚。
- 附带 Hermes Skill 和中文操作手册。
- 实际密钥和线上策略不进入公开仓库。

## 只读 Web 管理台

服务器部署后可通过 `/cpa-admin/` 访问只读仪表盘，查看模型目录、下游 Key 权限、聚合池渠道和服务健康状态。

- 独立管理员 Basic Auth，不使用下游 Key。
- 静态页面加本机只读 JSON API，无数据库和后台计算任务。
- API 不读取或返回密钥材料。
- 不提供写操作；正式修改仍通过 `cpa-admin` 草稿、审批和发布流程。

## 文档

- [中文操作手册](docs/manual.zh.md)
- [Hermes Skill](skills/cpa-admin/SKILL.md)
- [权限配置示例](examples/access.yaml)
- [模型配置示例](examples/model.yaml)
- [聚合池配置示例](examples/pools.yaml)

## 让 Hermes 安装管理能力

把下面这段自然语言直接发给服务器上的 Hermes：

```text
请为这台服务器安装 CPA 管理 CLI 和 Hermes Skill，仓库地址是：
https://github.com/BakaXXXXXL/xiaoye-cpa-admin

要求：
1. 将仓库克隆到一个临时目录，阅读 README.md 和 docs/manual.zh.md。
2. 确认 Python 3 和 PyYAML 可用；缺少依赖时先告诉我，不要自行更换系统 Python。
3. 将 server/cpa_admin.py 安装为 /opt/cpa-admin/cpa_admin.py，并在 /usr/local/bin/cpa-admin 建立命令链接。
4. 将 skills/cpa-admin/SKILL.md 安装到 Hermes 的 skills/cpa-admin/SKILL.md。
5. 保留服务器现有的 access.yaml、secrets.json、catalog 和 model-capabilities.json，不读取或输出任何密钥，不覆盖现有权限配置。
6. 安装过程不要执行 cpa-admin apply、rollback，不要重启 CPA 服务，也不要创建或轮换 Key。
7. 安装后运行 cpa-admin --help；如果服务器已有统一配置，再运行 cpa-admin validate 做只读验证。
8. 最后告诉我 CLI 和 Skill 的安装路径、验证结果，以及以后可以用哪些自然语言让你协助管理模型和 Key 权限。
9. 清理临时克隆目录，不把任何服务器私有配置上传回公开仓库。
```

安装后可以这样说：

```text
列出 CPA 当前所有模型和每把 Key 的权限。
给 friend 增加 qwen-payg/Qwen4-Max 的使用权限，但先不要发布；校验后把 diff 给我看。
新增一个模型配置草稿，我需要你先检查字段是否合法，不要执行 apply。
```

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
