# Gerrit 代码审查插件

为 Codex 增加独立的 Gerrit 审查工作流：粘贴变更链接即可读取补丁集、差异和完整代码上下文，交由 Codex 分析。使用本机现有 SSH 密钥，不保存密码。

本插件以工具与审查技能的形式运行，审查结论显示在聊天中；不修改客户端，也不向内置 GitHub/GitLab 列表注入提供商。

## 依赖与安装

需要 Linux/macOS、本机 Python 3.7+、Git、OpenSSH 和可访问 Gerrit 的现有 SSH 认证。仅使用 Python 标准库，无需 pip 安装依赖。Windows 原生运行暂未验证（文件锁使用 POSIX `fcntl`）。

从 GitHub 安装：

```sh
git clone https://github.com/Mars-cb/gerrit-review.git
cd gerrit-review
```

在克隆后的目录执行：

```sh
codex plugin marketplace add .
codex plugin add gerrit-review@gerrit-review-local
```

若 `codex` 不在 PATH，使用已安装的 Codex 可执行文件完整路径。

## 配置服务器

以实际服务器地址和 SSH 用户名执行，密码不属于配置项：

```sh
python3 plugins/gerrit-review/server.py configure \
  --name company \
  --web-url https://gerrit.example.com \
  --ssh-host gerrit.example.com \
  --ssh-port 29418 \
  --ssh-user your-user
```

默认写入 `$XDG_CONFIG_HOME/gerrit-review/config.json`（通常是 `~/.config/gerrit-review/config.json`），保留已有其他服务器条目。可用 `GERRIT_REVIEW_CONFIG` 指定配置文件。支持多个具名服务器，默认由 `default_server` 决定；工具可传 `server` 指定配置。

SSH 使用 `BatchMode=yes` 和 `StrictHostKeyChecking=yes`。先通过正常 SSH 流程配置可信主机和密钥；插件不会自动接受主机指纹或提示输入密码。

## 在 Codex 中使用

新聊天中选择 **Gerrit 代码审查**，输入：

```text
审查 https://gerrit.example.com/c/project/+/12345
重点检查并发、兼容性和错误处理，先只在聊天里给出结论。
```

也支持带补丁集的链接，例如 `/+/12345/3`。审查固定在对应完整提交 SHA；读取过程中出现新补丁集时，不会悄悄混入新代码。技能会让 Codex 检查相关函数、调用者、测试和项目审查规则。

首次安装后的工具加载取决于客户端，可能需要新开聊天或重启。以下 CLI 可单独验证连接：

```sh
python3 plugins/gerrit-review/server.py call gerrit_connection_status
python3 plugins/gerrit-review/server.py call gerrit_get_change --args '{"change":"12345"}'
```

## 工具

| 工具 | 功能 |
| --- | --- |
| `gerrit_connection_status` | 检查 SSH 连接、Gerrit 版本和发布开关 |
| `gerrit_get_change` | 读取变更、补丁集、完整 revision 和文件列表 |
| `gerrit_get_diff` | 固定版本 diff，可按文件和 diff 文本行分页 |
| `gerrit_get_file` | 读取新/旧版本代码，返回真实源码行号 |
| `gerrit_search_code` | 在同一提交的整个仓库搜索调用者或测试 |
| `gerrit_get_comments` | 分页读取历史审查消息和对应补丁集的行内评论 |
| `gerrit_prepare_review` | 校验行号并保存本地预览，返回草稿 ID 和摘要 |
| `gerrit_publish_review` | 仅发布已预览且获明确授权的草稿 |

## 发布评论

默认 `allow_publish=false`。审查和生成预览不会写 Gerrit。需要该能力时，由用户启用：

```sh
python3 plugins/gerrit-review/server.py configure \
  --name company --web-url https://gerrit.example.com \
  --ssh-host gerrit.example.com --ssh-user your-user \
  --enable-publishing
```

发布前要求最新补丁集、匹配预览内容的摘要、未过期且未发布的本地草稿；变更已合并或放弃时拒绝发布。评论默认不发邮件，不打分、不提交合并、不改标签，并保留其他现有草稿。先做最新补丁集检查再按 SHA 发布；网络并发下仍可能在检查之后出现新补丁集，但评论始终绑定原 SHA，不会贴到另一个版本。

发布后重复调用同一草稿会返回已有回执。若网络结果不确定，草稿进入待核对状态，不自动重发，避免重复评论。此功能的真实写入需要用户明确授权；开发验证不会向真实变更发布测试评论。

## 本地数据

私有 bare Git 缓存、锁和预览草稿保存在 `PLUGIN_DATA`；未提供时保存到 `$XDG_DATA_HOME/codex-gerrit-review`（通常是 `~/.local/share/codex-gerrit-review`）。也可用 `GERRIT_REVIEW_STATE` 指定目录。目录权限为 0700，草稿权限为 0600。

缓存包含私有源码，草稿包含审查正文，应按项目数据管理。它们不属于插件源代码，不应放入分发包或公共仓库。缓存按服务器、账号和项目隔离。卸载后如不再需要，可以删除这些本机数据。

插件不运行仓库代码，也不修改用户工作树。Git 外部 diff/textconv 和 hooks 被禁用，Git 文件路径按字面量处理。工具不输出私钥、密码或 SSH 环境。

单文件读取限 2 MiB UTF-8 文本；二进制文件仅返回元数据。差异、源码和评论均支持分页，过大的响应会要求缩小范围，不会假装已经审查完整内容。

## 开发验证

```sh
python3 -m unittest discover -s plugins/gerrit-review -p 'test_*.py'
```

单元测试使用本地临时仓库和模拟 Gerrit，不需要账号，不向真实服务器写评论。真实环境的只读连接、固定版本 diff 和文件读取另行验证。

## Actions 与 Release

`main` 提交和 Pull Request 会在 Python 3.11、3.13 上运行测试、验证清单并构建发布包。CI 使用本地测试数据，不需要 Gerrit 凭据。

在 GitHub 的 **Actions → Release → Run workflow** 中可以直接发布版本；也可推送 `v0.1.0` 这类版本标签触发。测试通过后自动上传独立插件 ZIP、市场安装 ZIP 和 SHA-256 校验文件，无需额外发布密钥。

版本需同时更新 `plugins/gerrit-review/plugin.json` 与 `gerrit.py` 的 `VERSION`，工作流会校验一致性。完整步骤见 [版本发布说明](docs/RELEASING.md)。下载入口为 [Releases](https://github.com/Mars-cb/gerrit-review/releases)。

本地构建同样的发布包：

```sh
python3 scripts/package_release.py
```

运行数据的保存与共享范围见 [数据与隐私](PRIVACY.md)。

## 卸载

```sh
codex plugin remove gerrit-review@gerrit-review-local
codex plugin marketplace remove gerrit-review-local
```

## 官方接口参考

- [Gerrit SSH 查询](https://gerrit-review.googlesource.com/Documentation/cmd-query.html)
- [Gerrit SSH 审查与 ReviewInput](https://gerrit-review.googlesource.com/Documentation/cmd-review.html)
- [Codex 插件打包](https://developers.openai.com/plugins/build/plugins)

本地插件包采用 MIT 许可证。这个本机插件没有提交至官方公开目录。
