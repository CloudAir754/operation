#!/bin/bash

# 非 root 直接退出
if [ $EUID -ne 0 ]; then
    echo -e "\033[31m错误：必须使用 root 权限运行！\033[0m"
    echo -e "\033[33m请使用：sudo $0\033[0m"
    exit 1
fi

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${YELLOW}===== 用户磁盘占用统计（按磁盘分区）=====${NC}"
echo -e "统计时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo -e "--------------------------------------------------------\n"

# 获取真实用户
REAL_USERS=$(awk -F: '$3>=1000 && $3!=65534 {print $1":"$6}' /etc/passwd)

while IFS=":" read -r username home_dir; do
    if [ ! -d "$home_dir" ]; then
        continue
    fi

    echo -e "${GREEN}【用户】$username${NC} | 家目录：$home_dir"

    # 获取分区设备（例如 /dev/sda1 /dev/sdb1）
    dev=$(df -P "$home_dir" | awk 'NR==2 {print $1}')
    # 获取挂载点
    mount_point=$(df -P "$home_dir" | awk 'NR==2 {print $NF}')

    echo -e "  💽 磁盘分区：${BLUE}$dev${NC}"
    echo -e "  📂 所在挂载：$mount_point"

    # 统计大小
    usage=$(du -sh "$home_dir" 2>/dev/null | awk '{print $1}')
    total=$(df -h --output=size "$mount_point" | awk 'NR==2 {print $1}')
    used=$(df -h --output=used "$mount_point" | awk 'NR==2 {print $1}')
    avail=$(df -h --output=avail "$mount_point" | awk 'NR==2 {print $1}')

    echo -e "  📊 占用空间：${RED}$usage${NC}"
    echo -e "  📦 分区容量：总 $total | 已用 $used | 可用 $avail"
    echo -e "--------------------------------------------------------\n"

done <<< "$REAL_USERS"

echo -e "${YELLOW}===== 系统物理硬盘 =====${NC}"
lsblk -d -o NAME,SIZE,TYPE,MODEL | grep disk
echo -e "${NC}"