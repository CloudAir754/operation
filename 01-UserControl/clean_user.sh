#!/bin/bash
set -eo pipefail  # 启用严格模式，脚本出错立即退出
USER="替换我" # 请替换为要清理的用户名
HOME_DIR="/home/$USER"

echo "=================================================="
echo "🧹 安全清理用户 $USER (支持挂载目录处理)"
echo "=================================================="

# ===================== 函数定义 =====================
# 检查目录是否为挂载点
is_mount_point() {
    local dir="$1"
    if mount | grep -q " ${dir} "; then
        return 0  # 是挂载点
    else
        return 1  # 不是挂载点
    fi
}

# ===================== 1. 强制终止用户所有进程 =====================
echo -e "\n💀 终止用户 $USER 所有进程..."
sudo pkill -9 -u "$USER" 2>/dev/null || true
sudo killall -9 -u "$USER" 2>/dev/null || true

# 等待进程释放文件句柄（避免挂载点繁忙）
sleep 3

# 彻底清理残留进程
REMAINING=$(ps -u "$USER" -o pid= 2>/dev/null | wc -l)
if (( REMAINING > 0 )); then
    echo "⚠️  检测到 $REMAINING 个残留进程，强制清理..."
    sudo ps -u "$USER" -o pid= | xargs -r sudo kill -9
    sleep 1
fi

# ===================== 2. 删除系统用户 =====================
echo -e "\n🗑️  删除系统用户 $USER..."
if id "$USER" &>/dev/null; then
    # 优先使用强制删除，失败则降级执行
    sudo userdel -rf "$USER" 2>/dev/null || sudo userdel -r "$USER" 2>/dev/null || true
else
    echo "ℹ️  用户 $USER 已不存在"
fi

# 验证用户删除结果
if id "$USER" &>/dev/null; then
    echo "❌ 用户删除失败"
else
    echo "✅ 用户已成功删除"
fi

# ===================== 3. 挂载目录专属处理（核心升级） =====================
echo -e "\n📂 处理家目录：$HOME_DIR"
if [[ -d "$HOME_DIR" ]]; then
    if is_mount_point "$HOME_DIR"; then
        echo "⚠️  检测到该目录是【挂载点】，执行安全卸载流程..."
        
        # 尝试优雅卸载
        sudo umount "$HOME_DIR" 2>/dev/null || true
        sleep 1
        
        # 优雅卸载失败 → 强制卸载
        if is_mount_point "$HOME_DIR"; then
            echo "🔨 优雅卸载失败，执行强制卸载..."
            sudo umount -lf "$HOME_DIR" 2>/dev/null
            sleep 1
        fi
        
        # 卸载完成后删除空目录
        if ! is_mount_point "$HOME_DIR"; then
            echo "🗑️  删除挂载点目录..."
            sudo rm -rf "$HOME_DIR"
        else
            echo "❌ 卸载失败，目录仍处于挂载状态，跳过删除（避免数据丢失）"
        fi
    else
        # 普通目录直接删除
        echo "🗑️  普通目录，直接删除..."
        sudo rm -rf "$HOME_DIR"
    fi
else
    echo "ℹ️  家目录已不存在"
fi

# ===================== 4. 最终校验 =====================
echo -e "\n✅ 最终结果校验"
if [[ -d "$HOME_DIR" ]]; then
    echo "❌ 家目录清理失败"
else
    echo "✅ 家目录已完全清理"
fi

echo -e "\n🎉 清理流程执行完成"