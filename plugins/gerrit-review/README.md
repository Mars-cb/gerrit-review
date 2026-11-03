# Gerrit 代码审查

本包是本机 stdio MCP 插件，需要 Python 3.7+、Git、OpenSSH，以及已建立的 Gerrit SSH 认证。无需 pip 依赖。

## 配置

在插件目录运行，将示例替换为实际地址和账号：

```sh
python3 server.py configure --name company --web-url https://gerrit.example.com --ssh-host gerrit.example.com --ssh-user your-user
python3 server.py call gerrit_connection_status
```

配置写入 XDG_CONFIG_HOME/gerrit-review/config.json，通常是 ~/.config/gerrit-review/config.json。也可用 GERRIT_REVIEW_CONFIG 指定路径。不需要密码字段，不会自动接受新的 SSH 主机密钥。

客户端安装此插件后，新聊天中选择“Gerrit 代码审查”，粘贴变更链接。模型使用固定 revision 读取差异、文件、调用者、测试和已有评论，审查结论显示在聊天与独立面板中。

支持 MCP Apps Extensions 的 Codex 中可从侧栏/聊天内容标签打开“Gerrit 审查面板”。面板打开后连接插件自己的专用 Codex 会话，已有会话直接复用。点击“在面板中审查”或用面板输入框发送要求、继续追问；公开回复和读取工具进度实时显示在面板中，不向当前聊天发送消息，不创建首页聊天。

本机浏览器入口：`python3 server.py panel`，打开命令打印的 URL，即可在相同会话内审查和追问。按 Ctrl-C 停止 HTTP 服务，后台会话保留；`python3 server.py session-stop` 可停止会话服务。系统/服务重启后保留本机记录，并以近期记录继续新的内存线程。

需要本机已登录的 Codex App Server；默认使用当前 Codex 模型配置及用量。找不到可执行文件时设置 GERRIT_REVIEW_CODEX 为完整路径。仅提供 Gerrit 读取和本机结论保存工具；专用会话不运行 shell、不发布 Gerrit 评论。HTTP 仅监听 127.0.0.1；若 MCP 使用 PLUGIN_DATA 或自定义目录，将浏览器服务的 GERRIT_REVIEW_STATE 指向相同目录以复用会话。

默认不发布评论。需要发布时，由用户通过 configure 的 --enable-publishing 参数启用，并明确要求向具体变更发布预览内容。发布工具不打分、不合并、不发送通知邮件。

私有源码缓存和本地预览、审查结果存储在 PLUGIN_DATA；未提供时在 ~/.local/share/codex-gerrit-review（可用 GERRIT_REVIEW_STATE 指定）。不要将这些运行数据打进插件包。

## 测试

```sh
python3 -m unittest discover -s . -p 'test_*.py'
node web/test_panel.cjs
```

完整市场安装包另附市场清单和安装说明。本独立 ZIP 根目录包含 portable plugin.json 和 mcp.json，可以作为插件包保存；本机进程仍需在桌面环境运行。
