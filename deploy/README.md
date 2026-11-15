# 部署（Linux）

三种方式，按约束从重到轻：

| 方式 | 用什么 | 隔离级别 |
|---|---|---|
| 容器 | `Dockerfile` + `compose.yml` | 进程级（推荐；与 ToolBackend 全容器执行同向） |
| systemd | `jagent.service` | 进程级（独立服务用户 + `ProtectSystem` 可自行加固） |
| 裸跑 | `uv sync && uv run python -m jagent.api` | 无（仅内网/本机） |

硬要求：

1. **bash 必须存在**：shell.exec 的执行底座是 `bash -c`（D-037）；镜像/systemd 环境都要保证；
   `python` 由 venv 提供（`_prepend_service_interpreter` 前置 PATH），裸系统跑 shell 工具前先确认。
2. **数据根显式化**：`JAGENT_DATA_DIR` 指向持久卷（容器是 `/app/data` 卷；systemd 是
   `/var/lib/jagent`）——默认值相对 CWD，只适合本地开发。
3. **本服务无鉴权**：`JAGENT_HOST=0.0.0.0` 只在反代/防火墙之后使用。
4. **面板**：镜像不含 ui/dist（构建产物不入库）——容器部署默认是内置单文件页；
   要完整面板在宿主 build（`cd ui && npm run build`）并挂载到 `/app/ui/dist`。
5. **启动日志第一行**可见绑定地址 / .env 来源 / 数据根 / 面板模式——四项任何一项
   不符合预期就地发现，不靠事后排查。
