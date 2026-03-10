# MAA Python Runtime

`maa python` 现在使用独立运行时目录，不再默认复用 `maa-cli` 的 library/resource。

默认目录：

```text
var/maa-python/
```

目录用途：

- `var/maa-python/bundle/`：自动下载并解压的官方 MAA 运行时包，里面包含 `MaaCore` 和 `resource`
- `var/maa-python/config/`：运行时实际读取的 MAA 配置，内容会从仓库内的 `Arknights/addons/contrib/maa/cli_config/maa/` 同步过去
- `var/maa-python/cache/`：缓存和热更新资源目录
- `var/maa-python/debug/`：MAA 用户目录下的日志/调试输出
- `var/maa-python/downloads/`：下载临时文件
- `var/maa-python/runtime.json`：最近一次下载后的运行时信息

自动行为：

- 首次执行 `maa_python` 任务时，如果缺少 `MaaCore` 或 `resource`，会自动下载官方 MAA 发布包并解压到 `var/maa-python/bundle/`
- 每次执行 `run_all_tasks_result()` 这条公共 MAA 任务链时，都会先检查一次远端版本；如果发现新版本，会先刷新本地运行时再继续执行任务
- 元数据优先走 `ota.maa.plus`，不可用时回退到 GitHub 官方 release API；优先查询 `MaaAssistantArknights/MaaAssistantArknights`，再查询旧的 `MaaRelease`
- Linux 下如果官方源拿不到可用运行时包，会把本机 `maa-cli` 的 `lib/resource` 复制到 `maa python` 自己的目录，避免继续直接依赖 `maa-cli` 的原始目录
- 默认下载通道是 `beta`

可用环境变量：

- `AH2_MAA_PYTHON_DIR`：覆盖默认运行时目录
- `AH2_MAA_PYTHON_CHANNEL`：切换下载通道，可选 `stable`、`beta`、`alpha`，默认 `beta`
- `AH2_MAA_PYTHON_CA_BUNDLE`：为下载流程指定 CA 证书文件
- `AH2_MAA_PYTHON_INSECURE`：设为 `1` 时跳过 TLS 证书校验，只建议在自签名代理环境下临时使用
- `AH2_MAA_CLI_PATH`：指定 Linux 兜底复制时使用的 `maa-cli` 可执行文件
- `MAA_DEVICE`：覆盖默认设备地址

手动预下载或查看路径：

```bash
uv run python maa_python_smoke.py --mode paths
uv run python maa_python_smoke.py --mode prepare
uv run python maa_python_smoke.py --mode prepare --force-download
```

说明：

- `paths` 只打印当前 `maa python` 预计使用的目录
- `prepare` 会确保运行时和配置已经准备好
- `prepare --force-download` 会重新下载官方运行时包
