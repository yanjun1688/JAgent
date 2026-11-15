# JAgent 服务镜像（Linux 部署的最小集）。
# 构建上下文 = 仓库根（公开树）；本地私有内容（docs/scripts/data/.env）不进镜像——
# 需要的运行时配置全部走环境变量 / 挂载（见 compose.yml 与 .env.example）。
#
#   docker build -f deploy/Dockerfile -t jagent:latest .
#   docker run --rm -p 8021:8021 -v jagent-data:/app/data --env-file .env jagent:latest
FROM python:3.11-slim

# bash：shell.exec 的执行底座（D-037）；git：run 指纹探测 + 白名单命令
RUN apt-get update && apt-get install -y --no-install-recommends bash git && rm -rf /var/lib/apt/lists/*

# uv：与仓库锁定文件一致的依赖安装
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md AGENTS.md ./
COPY jagent ./jagent
COPY tests ./tests
RUN uv sync --frozen --no-dev

# 数据根挂载点（compose 里映射卷；默认落在 /app/data）
ENV JAGENT_DATA_DIR=/app/data
EXPOSE 8021

CMD [".venv/bin/python", "-m", "jagent.api"]
