#!/bin/bash
# create_users_v2.sh
# 批量创建Linux用户脚本（优化版：含强密码生成、自动权限修复、真实姓名关联）
# 使用前提：1. 以root权限执行 2. 提前准备user_list.txt文件

# ===================== 配置项 ======================
USER_LIST_FILE="user_list.txt"
PASSWORD_LOG_FILE="user_passwords.log"
MIN_PWD_LEN=16
MAX_PWD_LEN=18

# ===================== 核心函数 =====================

# 生成指定长度范围的随机强密码 (修复了长度可能不足的Bug)
generate_random_password() {
    local pwd_len=$((RANDOM % (MAX_PWD_LEN - MIN_PWD_LEN + 1) + MIN_PWD_LEN))
    local password=""
    local chars='A-Za-z2-9!@#$%^&*()_+-='
    
    # 循环直到生成足够长度的密码
    while [ ${#password} -lt $pwd_len ]; do
        # 生成一段随机字符串并过滤
        local segment=$(openssl rand -base64 48 | tr -dc "$chars")
        password="${password}${segment}"
    done
    
    # 截取精确长度
    echo "${password:0:$pwd_len}"
}

# 修复用户目录权限 (整合了你提供的第二段脚本逻辑)
fix_user_permissions() {
    local username=$1
    local userdir="/home/$username"
    
    if [ ! -d "$userdir" ]; then
        echo "⚠️ 警告：用户 $username 的家目录 $userdir 不存在，跳过权限修复。"
        return 1
    fi

    # 重置目录所有者为该用户及其主组
    chown -R "$username:$username" "$userdir"
    
    # 设置HOME目录为700权限（仅所有者可读写执行，更安全）
    chmod 700 "$userdir"
    
    # 特殊处理：如果存在 .vscode-server 或其他隐藏开发目录，确保权限正确
    # 虽然新用户通常没有，但这能防止后续工具创建时的权限隐患
    if [ -d "$userdir/.vscode-server" ]; then
        chown -R "$username:$username" "$userdir/.vscode-server"
        chmod -R 700 "$userdir/.vscode-server"
    fi
    
    return 0
}

# ===================== 主逻辑 =====================

# 1. 检查 Root 权限
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 错误：必须以 root 权限执行此脚本！请使用 sudo ./script.sh"
    exit 1
fi

# 2. 检查用户列表文件
if [ ! -f "$USER_LIST_FILE" ]; then
    echo "❌ 错误：用户列表文件 $USER_LIST_FILE 不存在！"
    echo "💡 提示：请创建该文件，格式为每行：用户名 真实姓名"
    exit 1
fi

# 3. 初始化日志文件
> "$PASSWORD_LOG_FILE"
chmod 0600 "$PASSWORD_LOG_FILE"
echo -e "用户名\t真实姓名\t密码\t创建时间" >> "$PASSWORD_LOG_FILE"

echo "🚀 开始批量创建用户..."

# 4. 循环处理用户
while read -r username realname; do
    # 跳过空行和注释
    [[ -z "$username" || "$username" =~ ^# ]] && continue

    # 检查用户是否存在
    if id "$username" &>/dev/null; then
        echo "⚠️ 用户 $username 已存在，跳过。"
        continue
    fi

    # 生成密码
    password=$(generate_random_password)

    # 创建用户
    # -m: 创建家目录, -s: 指定shell, -c: 注释字段(真实姓名)
    if ! useradd -m -s /bin/bash -c "$realname" "$username"; then
        echo "❌ 创建用户 $username 失败，跳过。"
        continue
    fi

    # 设置密码
    if ! echo "$username:$password" | chpasswd; then
        echo "❌ 为用户 $username 设置密码失败，正在回滚删除用户..."
        userdel -r "$username"
        continue
    fi

    # 【关键步骤】立即修复权限 (整合了你的第二段脚本逻辑)
    if fix_user_permissions "$username"; then
        echo "✅ 权限已自动修复：/home/$username (Owner: $username, Mode: 700)"
    else
        echo "⚠️ 权限修复步骤异常，请手动检查 /home/$username"
    fi

    # 记录日志
    create_time=$(date "+%Y-%m-%d %H:%M:%S")
    echo -e "$username\t$realname\t$password\t$create_time" >> "$PASSWORD_LOG_FILE"

    echo "✅ 成功创建：$username ($realname)"

done < "$USER_LIST_FILE"

# ===================== 收尾 =====================
echo -e "\n🎉 批量操作完成！"
echo "🔒 密码文件位置：$PASSWORD_LOG_FILE (权限 0600)"
echo "💡 安全建议：请在分发密码后尽快删除或加密备份此文件。"