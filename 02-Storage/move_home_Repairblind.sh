#!/usr/bin/env bash
set -euo pipefail
# move_home_Repairblind.sh
# 用于恢复v1版本迁移后出现的家目录无法访问问题（Bind Mount 方式）

# ==============================================================================
# 配置区域
# ==============================================================================
USERNAME="Username" # 替换为需要修复的用户名
NEW_BASE="/mnt/data/home_2"

# 是否将 /etc/passwd 里的家目录恢复为 /home/Username
# true = /home/Username (推荐，配合 Bind Mount)
# false = /mnt/data/home_2/Username (不推荐，可能导致权限问题)
RESTORE_PASSWD_TO_HOME=true

# ==============================================================================
# 变量准备
# ==============================================================================
OLD_HOME="/home/$USERNAME"
NEW_HOME="$NEW_BASE/$USERNAME"
DATE_STR=$(date +%F_%H%M%S)
BACKUP_DIR="/root/home_repair_backup_$DATE_STR"

echo "------------------------------------------------"
echo "🚀 开始执行 Home 目录修复脚本"
echo "👤 用户: $USERNAME"
echo "📂 目标: $NEW_HOME -> $OLD_HOME"
echo "------------------------------------------------"

# 1. 基础检查
if [[ $EUID -ne 0 ]]; then
    echo "❌ 错误: 必须以 root 权限运行此脚本。"
    exit 1
fi

if ! id "$USERNAME" &>/dev/null; then
    echo "❌ 错误: 系统中不存在用户 $USERNAME。"
    exit 1
fi

if [[ ! -d "$NEW_HOME" ]]; then
    echo "❌ 错误: 新的 Home 目录不存在: $NEW_HOME"
    exit 1
fi

# 主动停止用户进程，防止后续操作被占用
pkill -u -9 "$USERNAME" 2>/dev/null || true

# 2. 进程占用检查 (防止 usermod 失败)
if pgrep -u "$USERNAME" > /dev/null; then
    echo "⚠️ 警告: 用户 $USERNAME 当前有进程正在运行！"
    echo "请先断开该用户的 SSH 连接或执行 'pkill -u $USERNAME'。"
    exit 1
fi

# 3. 备份关键配置
echo "📦 备份系统文件至 $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp /etc/passwd "$BACKUP_DIR/passwd.bak"
cp /etc/fstab "$BACKUP_DIR/fstab.bak"

# 4. 修复 /home 挂载点
echo "🔧 处理挂载路径..."

# 如果旧路径是软链接，先删除
if [[ -L "$OLD_HOME" ]]; then
    echo "🗑️ 发现旧的软链接，正在移除..."
    rm -f "$OLD_HOME"
fi

# 创建挂载点（如果不存在）
if [[ ! -d "$OLD_HOME" ]]; then
    mkdir -p "$OLD_HOME"
fi

# 执行 Bind Mount
if mountpoint -q "$OLD_HOME"; then
    echo "✅ $OLD_HOME 已经是挂载点，跳过手动挂载。"
else
    echo "🔗 执行 Bind Mount..."
    mount --bind "$NEW_HOME" "$OLD_HOME"
fi

# 5. 更新 /etc/fstab (防止重复写入)
echo "📝 更新 /etc/fstab..."
FSTAB_LINE="$NEW_HOME $OLD_HOME none bind 0 0"
# 转义路径用于 grep 匹配
ESCAPED_NEW=$(echo "$NEW_HOME" | sed 's/\//\\\//g')
ESCAPED_OLD=$(echo "$OLD_HOME" | sed 's/\//\\\//g')

if grep -qE "^\s*$ESCAPED_NEW\s+$ESCAPED_OLD\s+" /etc/fstab; then
    echo "ℹ️ fstab 中已存在对应条目，跳过。"
else
    echo "$FSTAB_LINE" >> /etc/fstab
    echo "✅ 已添加挂载条目至 fstab。"
fi

# 6. 修复 /etc/passwd
echo "👤 修复用户家目录指向..."
TARGET_DIR_IN_PASSWD="$NEW_HOME"
[[ "$RESTORE_PASSWD_TO_HOME" == true ]] && TARGET_DIR_IN_PASSWD="$OLD_HOME"

CURRENT_DIR_IN_PASSWD=$(getent passwd "$USERNAME" | cut -d: -f6)

if [[ "$CURRENT_DIR_IN_PASSWD" == "$TARGET_DIR_IN_PASSWD" ]]; then
    echo "ℹ️ /etc/passwd 指向正确 ($TARGET_DIR_IN_PASSWD)，无需修改。"
else
    usermod -d "$TARGET_DIR_IN_PASSWD" "$USERNAME"
    echo "✅ 用户家目录已更改为: $TARGET_DIR_IN_PASSWD"
fi

# 7. 权限修复
echo "🔐 修复目录所有权与权限..."
chown -R "$USERNAME:$USERNAME" "$NEW_HOME"
chmod 700 "$NEW_HOME"

# 8. SELinux 修复 (如果启用)
if command -v selinuxenabled &>/dev/null && selinuxenabled; then
    echo "🔒 修复 SELinux 上下文..."
    # 设置路径等效性
    if command -v semanage &>/dev/null; then
        semanage fcontext -a -e "$OLD_HOME" "$NEW_HOME" || true
    fi
    # 递归恢复上下文，-T 0 使用多线程加速
    restorecon -Rv -T 0 "$NEW_HOME"
    restorecon -Rv -T 0 "$OLD_HOME"
fi

# 9. 刷新系统状态
echo "🔄 刷新 systemd 守护进程..."
systemctl daemon-reload
systemctl daemon-reexec || true

# 10. 验证结果
echo "🧪 执行最终验证..."
SU_TEST=$(su - "$USERNAME" -c 'pwd' 2>/dev/null || echo "FAILED")

if [[ "$SU_TEST" == "$TARGET_DIR_IN_PASSWD" ]]; then
    echo "✅ 登录测试成功: $SU_TEST"
else
    echo "❌ 登录测试失败，实际路径: $SU_TEST"
fi

echo "------------------------------------------------"
echo "✨ 修复完成！"
echo "📂 备份记录位于: $BACKUP_DIR"
echo "💡 建议重启系统以确保所有服务正常识别挂载点。"
echo "------------------------------------------------"