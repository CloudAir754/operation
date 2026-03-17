#!/usr/bin/env bash
set -euo pipefail
# move_home2.sh
# 这是一个全新的迁移脚本，专门针对 v2 版本设计，采用 Bind Mount 方式实现家目录迁移。

# ================= 配置 =================
USERNAME="UserName" # 替换为要迁移的用户名
TARGET_BASE="/mnt/data/home_2"
DRY_RUN=false # true: 仅模拟迁移，不执行实际操作
# ========================================

OLD_HOME="/home/$USERNAME"
NEW_HOME="$TARGET_BASE/$USERNAME"
BACKUP_HOME="${OLD_HOME}.bak.$(date +%F_%H%M%S)"
LOG_FILE="/var/log/home_migration_${USERNAME}_$(date +%F_%H%M%S).log"

# 1. 日志初始化
touch "$LOG_FILE" 2>/dev/null || LOG_FILE="./migration.log"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "==== Home Migration (Bind Mount Mode) ===="
echo "User: $USERNAME"
echo "From: $OLD_HOME"
echo "To  : $NEW_HOME"
echo "Log : $LOG_FILE"
echo "========================================="

# 2. 权限与前置检查
[[ $EUID -ne 0 ]] && { echo "❌ 错误: 必须以 root 权限运行"; exit 1; }
id "$USERNAME" &>/dev/null || { echo "❌ 错误: 用户 $USERNAME 不存在"; exit 1; }
[[ -d "$OLD_HOME" ]] || { echo "❌ 错误: 原目录 $OLD_HOME 不存在"; exit 1; }

# 检查是否已经是挂载点，防止重复迁移
if mountpoint -q "$OLD_HOME"; then
    echo "⚠️ 警告: $OLD_HOME 已经是一个挂载点，迁移可能已完成或处于异常状态。中止。"
    exit 1
fi

if [[ -e "$NEW_HOME" ]]; then
    echo "❌ 错误: 目标路径 $NEW_HOME 已存在，请手动清理后再试。"; exit 1;
fi

# 3. 磁盘空间检查 (POSIX 兼容模式)
echo "💾 检查磁盘空间..."
# 获取原目录大小 (KB)
OLD_SIZE=$(du -s "$OLD_HOME" | awk '{print $1}')
# 获取目标挂载点剩余空间 (KB)
# 使用 -P 确保输出不换行
NEW_AVAIL=$(df -P "$TARGET_BASE" | tail -1 | awk '{print $4}')

# 预留 5% 的缓冲空间
REQUIRED_SIZE=$(( OLD_SIZE * 105 / 100 ))

if (( REQUIRED_SIZE > NEW_AVAIL )); then
    echo "❌ 错误: 目标磁盘空间不足！"
    echo "需要约: $((REQUIRED_SIZE/1024)) MB, 可用: $((NEW_AVAIL/1024)) MB"
    exit 1
fi

# 4. 彻底停止用户进程
echo "🔪 停止用户进程..."
loginctl terminate-user "$USERNAME" 2>/dev/null || true
pkill -15 -u "$USERNAME" 2>/dev/null || true
sleep 2
pkill -9 -u "$USERNAME" 2>/dev/null || true

# ==============================
# ✅ 修复：只检查【当前用户】进程，忽略系统/kernel/root 占用
# ==============================
# ✅ 干净检查：只判断用户进程，不输出内核乱码
if command -v fuser &>/dev/null; then
  USER_UID=$(id -u "$USERNAME" 2>/dev/null)
  if [ -n "$USER_UID" ] && fuser -v -m "$OLD_HOME" 2>/dev/null | awk '{print $3}' | grep -qw "$USER_UID"; then
    echo "❌ 错误：用户 $USERNAME 仍有进程占用目录，迁移中止！"
    exit 1
  fi
fi

echo "✅ 进程清理完成，开始迁移"

# 5. 同步数据
echo "📦 同步数据中 (rsync)..."
mkdir -p "$NEW_HOME"
# -aHAXS: 归档、硬链接、ACL、扩展属性、稀疏文件
# --numeric-ids: 避免映射 UID/GID（对迁移至关重要）
RSYNC_OPTS=(-aHAXS --numeric-ids --delete --info=progress2)
$DRY_RUN && RSYNC_OPTS+=(--dry-run)

rsync "${RSYNC_OPTS[@]}" "$OLD_HOME"/ "$NEW_HOME"/

# 确保顶层目录权限与原目录一致
chown --reference="$OLD_HOME" "$NEW_HOME"
chmod --reference="$OLD_HOME" "$NEW_HOME"

# 6. SELinux 处理 (如果启用)
if command -v selinuxenabled &>/dev/null && selinuxenabled; then
    echo "🔒 配置 SELinux 上下文..."
    if command -v semanage &>/dev/null; then
        # 将原目录的安全上下文等效应用到新路径
        semanage fcontext -a -e "$OLD_HOME" "$NEW_HOME" || true
        restorecon -Rv "$NEW_HOME"
    else
        echo "⚠️ 找不到 semanage，仅执行 restorecon。"
        restorecon -Rv "$NEW_HOME"
    fi
fi

# 7. 切换目录 (关键变更点)
if $DRY_RUN; then
    echo "跳过目录切换 (Dry Run Mode)"
else
    echo "🔁 正在切换至 Bind Mount 模式..."
    
    # 重命名原目录作为备份
    mv "$OLD_HOME" "$BACKUP_HOME"
    
    # 创建挂载锚点
    mkdir "$OLD_HOME"
    chown --reference="$BACKUP_HOME" "$OLD_HOME"
    chmod --reference="$BACKUP_HOME" "$OLD_HOME"

    # 执行挂载
    mount --bind "$NEW_HOME" "$OLD_HOME" || { echo "❌ 错误: 无法挂载 $NEW_HOME 至 $OLD_HOME"; exit 1; }

    # 写入 fstab (先检查是否已存在，防止重复)
    FSTAB_ENTRY="$NEW_HOME $OLD_HOME none bind 0 0"
    if ! grep -q "$OLD_HOME" /etc/fstab; then
        echo "$FSTAB_ENTRY" >> /etc/fstab
        echo "✅ 已添加挂载条目至 /etc/fstab"
    else
        echo "⚠️ /etc/fstab 中已存在相关条目，跳过追加。"
    fi
fi

# 8. 最终验证
echo "🧪 验证中..."
if mount | grep -q "$OLD_HOME"; then
    echo "✅ 挂载成功"
    # 测试用户是否可以正常进入（如果不是 Dry Run）
    if ! $DRY_RUN; then
        su - "$USERNAME" -c "ls -ld ~" || echo "⚠️ 警告: 用户切换测试失败，请检查。"
    fi
else
    echo "❌ 严重错误: 挂载未生效！"
    exit 1
fi

echo "======================================"
echo "✨ 迁移任务圆满完成！"
echo "原始数据备份在: $BACKUP_HOME"
echo "确认一切正常后，您可以手动删除该备份目录。"
echo "======================================"