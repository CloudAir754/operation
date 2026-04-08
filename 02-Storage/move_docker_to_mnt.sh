#!/usr/bin/env bash
set -euo pipefail

# move_docker_to_mnt.sh
# 将 Docker 数据目录迁移到 /mnt/data/docker，并通过 bind mount 保持路径不变。

# ================= 配置 =================
SRC_DOCKER_DIR="/var/lib/docker"
DST_DOCKER_DIR="/mnt/data/docker"
DRY_RUN=false # true: 仅模拟，不执行真实变更
# ========================================

BACKUP_DOCKER_DIR="${SRC_DOCKER_DIR}.bak.$(date +%F_%H%M%S)"
LOG_FILE="/var/log/docker_migration_$(date +%F_%H%M%S).log"

touch "$LOG_FILE" 2>/dev/null || LOG_FILE="./docker_migration.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "==== Docker Data Migration (Bind Mount Mode) ===="
echo "From: $SRC_DOCKER_DIR"
echo "To  : $DST_DOCKER_DIR"
echo "Log : $LOG_FILE"
echo "==============================================="

if [[ $EUID -ne 0 ]]; then
    echo "❌ 错误: 必须以 root 权限运行"
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "❌ 错误: 未检测到 rsync，请先安装后重试"
    exit 1
fi

if [[ ! -d "$SRC_DOCKER_DIR" ]]; then
    echo "❌ 错误: 源目录不存在: $SRC_DOCKER_DIR"
    exit 1
fi

if [[ "$DST_DOCKER_DIR" == "$SRC_DOCKER_DIR" || "$DST_DOCKER_DIR" == "$SRC_DOCKER_DIR"/* ]]; then
    echo "❌ 错误: 目标目录不能等于或位于源目录内部"
    exit 1
fi

if mountpoint -q "$SRC_DOCKER_DIR"; then
    echo "⚠️ 警告: $SRC_DOCKER_DIR 已经是挂载点，可能已迁移。中止。"
    exit 1
fi

if [[ -e "$DST_DOCKER_DIR" ]]; then
    echo "❌ 错误: 目标路径已存在: $DST_DOCKER_DIR"
    echo "请确认后手动清理该目录，再重新执行脚本。"
    exit 1
fi

echo "💾 检查磁盘空间..."
SRC_SIZE=$(du -s "$SRC_DOCKER_DIR" | awk '{print $1}')
DST_AVAIL=$(df -P "$(dirname "$DST_DOCKER_DIR")" | tail -1 | awk '{print $4}')
REQUIRED_SIZE=$(( SRC_SIZE * 105 / 100 ))

if (( REQUIRED_SIZE > DST_AVAIL )); then
    echo "❌ 错误: 目标磁盘空间不足"
    echo "需要约: $((REQUIRED_SIZE/1024)) MB, 可用: $((DST_AVAIL/1024)) MB"
    exit 1
fi

stop_service_if_exists() {
    local svc="$1"
    if systemctl list-unit-files | awk '{print $1}' | grep -qx "$svc"; then
        if systemctl is-active --quiet "$svc"; then
            echo "🔪 停止服务: $svc"
            systemctl stop "$svc"
        else
            echo "ℹ️ 服务未运行: $svc"
        fi
    fi
}

start_service_if_exists() {
    local svc="$1"
    if systemctl list-unit-files | awk '{print $1}' | grep -qx "$svc"; then
        echo "🚀 启动服务: $svc"
        systemctl start "$svc"
    fi
}

echo "🔒 停止 Docker 相关服务..."
stop_service_if_exists docker.service
stop_service_if_exists docker.socket
stop_service_if_exists containerd.service

echo "📦 同步数据中 (rsync)..."
mkdir -p "$DST_DOCKER_DIR"
RSYNC_OPTS=(-aHAXS --numeric-ids --delete --info=progress2)
$DRY_RUN && RSYNC_OPTS+=(--dry-run)

rsync "${RSYNC_OPTS[@]}" "$SRC_DOCKER_DIR/" "$DST_DOCKER_DIR/"

chown --reference="$SRC_DOCKER_DIR" "$DST_DOCKER_DIR"
chmod --reference="$SRC_DOCKER_DIR" "$DST_DOCKER_DIR"

if $DRY_RUN; then
    echo "✅ Dry Run 完成，未执行目录切换"
    exit 0
fi

echo "🔁 切换到 Bind Mount 模式..."
mv "$SRC_DOCKER_DIR" "$BACKUP_DOCKER_DIR"
mkdir "$SRC_DOCKER_DIR"
chown --reference="$BACKUP_DOCKER_DIR" "$SRC_DOCKER_DIR"
chmod --reference="$BACKUP_DOCKER_DIR" "$SRC_DOCKER_DIR"

mount --bind "$DST_DOCKER_DIR" "$SRC_DOCKER_DIR"

FSTAB_ENTRY="$DST_DOCKER_DIR $SRC_DOCKER_DIR none bind 0 0"
if ! grep -q "^[[:space:]]*$DST_DOCKER_DIR[[:space:]]\+$SRC_DOCKER_DIR[[:space:]]\+none[[:space:]]\+bind" /etc/fstab; then
    echo "$FSTAB_ENTRY" >> /etc/fstab
    echo "✅ 已写入 /etc/fstab"
else
    echo "⚠️ /etc/fstab 已存在同类条目，跳过写入"
fi

echo "🧪 验证挂载..."
if mountpoint -q "$SRC_DOCKER_DIR"; then
    echo "✅ 挂载成功"
else
    echo "❌ 错误: 挂载验证失败"
    echo "请检查并按需回滚:"
    echo "1) umount $SRC_DOCKER_DIR"
    echo "2) rm -rf $SRC_DOCKER_DIR"
    echo "3) mv $BACKUP_DOCKER_DIR $SRC_DOCKER_DIR"
    exit 1
fi

echo "🚀 恢复 Docker 相关服务..."
start_service_if_exists containerd.service
start_service_if_exists docker.socket
start_service_if_exists docker.service

echo "======================================"
echo "✨ Docker 目录迁移完成"
echo "备份目录: $BACKUP_DOCKER_DIR"
echo "确认 Docker 正常后，可手动删除备份目录"
echo "======================================"