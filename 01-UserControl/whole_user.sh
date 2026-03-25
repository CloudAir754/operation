#!/bin/bash
# 这个脚本用于列出所有用户
# 默认仅显示可登录的普通用户；传入 --all 可显示全部系统账户。

set -euo pipefail

PASSWD_FILE="/etc/passwd"
LOGIN_DEFS_FILE="/etc/login.defs"
SHOW_ALL=0

usage() {
	echo "用法: bash ./01-UserControl/whole_user.sh [--all]"
	echo "  --all   显示全部账户（含系统账户）"
}

for arg in "$@"; do
	case "$arg" in
		--all)
			SHOW_ALL=1
			;;
		-h|--help)
			usage
			exit 0
			;;
		*)
			echo "错误: 不支持的参数 $arg"
			usage
			exit 1
			;;
	esac
done

if [ ! -r "$PASSWD_FILE" ]; then
	echo "错误: 无法读取 $PASSWD_FILE"
	exit 1
fi

# 优先使用系统定义的 UID_MIN，缺失时回退到 1000。
UID_MIN=$(awk '/^UID_MIN[[:space:]]+/ {print $2; exit}' "$LOGIN_DEFS_FILE" 2>/dev/null || true)
if [ -z "${UID_MIN}" ]; then
	UID_MIN=1000
fi

printf "%-20s %-8s %-8s %-28s %-12s %-12s %s\n" "USERNAME" "UID" "GID" "HOME" "REALNAME" "DIR SIZE" "DISK"
printf "%-20s %-8s %-8s %-28s %-12s %-12s %s\n" "--------" "---" "---" "----" "--------" "--------" "----"

count=0
while IFS=: read -r username _ uid gid gecos home shell; do
	if [ "$SHOW_ALL" -eq 0 ]; then
		# 过滤系统账户和不可登录账户。
		if [ "$uid" -lt "$UID_MIN" ]; then
			continue
		fi
		if [ "$shell" = "/usr/sbin/nologin" ] || [ "$shell" = "/sbin/nologin" ] || [ "$shell" = "/bin/false" ]; then
			continue
		fi
	fi

	realname=${gecos%%,*}
	if [ -z "$realname" ]; then
		realname="-"
	fi

	# 解析真实目录路径
	real_home=$(readlink -f "$home" 2>/dev/null || echo "$home")

	if [ -d "$real_home" ]; then
		dir_size=$(du -sh "$real_home" | awk '{print $1}')
		disk=$(df "$real_home" | awk 'NR==2 {print $1}')
	else
		dir_size="N/A"
		disk="N/A"
	fi

	printf "%-20s %-8s %-8s %-28s %-12s %-12s %s\n" "$username" "$uid" "$gid" "$real_home" "$realname" "$dir_size" "$disk"
	count=$((count + 1))
done < "$PASSWD_FILE"

echo
if [ "$SHOW_ALL" -eq 1 ]; then
	echo "统计: 共 $count 个账户（全部）"
else
	echo "统计: 共 $count 个普通可登录账户"
fi
