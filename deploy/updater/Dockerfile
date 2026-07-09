FROM python:3.12-alpine

RUN apk add --no-cache bash ca-certificates curl docker-cli docker-cli-compose git openssh-client

# updater 以 root 在挂载的宿主项目目录（属主通常是普通用户）执行 git，
# 不显式信任会因 "dubious ownership" 直接失败。写入系统级 /etc/gitconfig：
# 与运行时 HOME 无关，且 git 的 safe.directory 只认系统/全局配置文件（不认 -c / 环境变量）。
RUN git config --system --add safe.directory /workspace

WORKDIR /workspace

COPY deploy/updater/server.py /app/server.py

ENV PYTHONUNBUFFERED=1

EXPOSE 8765

CMD ["python", "/app/server.py"]
