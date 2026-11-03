# 版本发布

## 自动检查

`main` 分支提交、Pull Request 或手动运行 CI 时，会在 Python 3.11、3.13 上执行测试，再验证清单和发布包校验值。测试使用临时 Git 仓库与模拟 Gerrit，不需要凭据，不会连接企业 Gerrit 或发布评论。

## 在 GitHub 页面发布

1. 更新 `plugins/gerrit-review/plugin.json` 和 `gerrit.py` 的 `VERSION`，提交到 `main`。
2. 打开仓库 **Actions → Release → Run workflow**，选择待发布分支。
3. `tag` 留空会读取清单版本，也可填 `v0.1.0` 这种与清单匹配的版本。需要草稿时勾选 `draft`。
4. 测试通过后，工作流使用仓库内置 `GITHUB_TOKEN` 创建 Release 和相应标签，附带两种 ZIP 和 SHA256SUMS。

无需配置额外发布密钥。只有发布任务具有 `contents: write` 权限。

## 推送标签发布

```sh
git tag v0.1.0
git push origin v0.1.0
```

标签版本与清单或代码版本不匹配时拒绝发布；标签已指向其他提交时拒绝替换。已有 Release 不会被覆盖，修改后应更新版本再发布。

## 发布产物

- `gerrit-review-vX.Y.Z.zip`：独立插件，根目录含 `plugin.json`、`mcp.json`，适合插件包导入。
- `gerrit-review-marketplace-vX.Y.Z.zip`：带市场清单的安装包，解压后可注册为本地市场。
- `SHA256SUMS`：校验上述文件。

包由显式文件列表构建，不包含本机 config.json、SSH 数据、源码缓存或审查草稿。它们是源码包，运行环境仍需 Python、Git 和 OpenSSH。

GitHub Release 与官方公开插件目录上架是不同流程。这个本机插件没有通过该工作流自动提交到官方公开目录。
