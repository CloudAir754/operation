#!/bin/bash
set -euo pipefail

# ===================== 配置项 =====================
SRC_DIR="/shared_data" # 源目录（例如 /home/youzirui/project）
DST_DIR="/mnt/data/shared_data" # 目标目录（例如 /mnt/data/project）
# ==================================================

LINK_PATH="${SRC_DIR}"

human_bytes() {
    local bytes="$1"
    if command -v numfmt >/dev/null 2>&1; then
        numfmt --to=iec --suffix=B "$bytes"
    else
        echo "${bytes}B"
    fi
}

# 检查是否以 root 运行
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 错误：必须使用 sudo 运行此脚本！"
    exit 1
fi

if ! command -v rsync >/dev/null 2>&1; then
    echo "❌ 错误：未找到 rsync，请先安装（Ubuntu: sudo apt install -y rsync）"
    exit 1
fi

# 检查配置项
if [ -z "${SRC_DIR}" ] || [ -z "${DST_DIR}" ]; then
    echo "❌ 错误：请先在脚本顶部配置 SRC_DIR 和 DST_DIR"
    exit 1
fi

# 防止把目录移动到自己里面
if [[ "${DST_DIR}" == "${SRC_DIR}" || "${DST_DIR}" == "${SRC_DIR}"/* ]]; then
    echo "❌ 错误：DST_DIR 不能等于或位于 SRC_DIR 内部"
    exit 1
fi

# 检查源目录是否存在
if [ ! -d "${SRC_DIR}" ]; then
    echo "❌ 错误：源目录不存在 → ${SRC_DIR}"
    exit 1
fi

# 检查目标目录不能已存在
if [ -e "${DST_DIR}" ]; then
    echo "❌ 错误：目标目录已存在，避免覆盖 → ${DST_DIR}"
    exit 1
fi

OWNER_UID_GID=$(stat -c '%u:%g' "${SRC_DIR}")
SRC_MODE=$(stat -c '%a' "${SRC_DIR}") # 获取源目录权限模式（八进制）

echo "✅ 所有检查通过，开始迁移..."

# 1. 创建目标目录
mkdir -p "$(dirname "${DST_DIR}")"

# 1.1 迁移前大小判断：目标分区可用空间需大于等于源目录大小
SRC_BYTES=$(du -sb "${SRC_DIR}" 2>/dev/null | awk '{print $1}')
if [ -z "${SRC_BYTES}" ] || ! [[ "${SRC_BYTES}" =~ ^[0-9]+$ ]]; then
    echo "❌ 错误：无法计算源目录大小 → ${SRC_DIR}"
    exit 1
fi

AVAIL_BYTES=$(df -PB1 "$(dirname "${DST_DIR}")" | awk 'NR==2 {print $4}')
if [ -z "${AVAIL_BYTES}" ] || ! [[ "${AVAIL_BYTES}" =~ ^[0-9]+$ ]]; then
    echo "❌ 错误：无法获取目标分区可用空间 → $(dirname "${DST_DIR}")"
    exit 1
fi

echo "📦 源目录大小：$(human_bytes "${SRC_BYTES}")"
echo "💽 目标可用空间：$(human_bytes "${AVAIL_BYTES}")"

if [ "${AVAIL_BYTES}" -lt "${SRC_BYTES}" ]; then
    echo "❌ 错误：目标空间不足，迁移已终止"
    echo "需要至少：$(human_bytes "${SRC_BYTES}")"
    echo "当前可用：$(human_bytes "${AVAIL_BYTES}")"
    exit 1
fi

# 2. 复制目录（显示进度）
echo "→ 复制（带进度）：${SRC_DIR} → ${DST_DIR}"
mkdir -p "${DST_DIR}"
rsync -aHAX --numeric-ids --info=progress2 --human-readable "${SRC_DIR}/" "${DST_DIR}/"

# 2.1 修正目标目录本身的元数据（rsync 上面复制的是内容，不会自动继承目录壳的权限）
chown "${OWNER_UID_GID}" "${DST_DIR}"
chmod "${SRC_MODE}" "${DST_DIR}"

# 2.2 复制成功后删除源目录
echo "→ 复制完成，清理源目录：${SRC_DIR}"
rm -rf "${SRC_DIR}"

# 源路径必须已腾空，才能创建软链接
if [ -e "${LINK_PATH}" ]; then
    echo "❌ 错误：移动后源路径仍存在，无法创建软链接 → ${LINK_PATH}"
    exit 1
fi

# 3. 创建软链接
echo "→ 创建软链接：${LINK_PATH} -> ${DST_DIR}"
ln -s "${DST_DIR}" "${LINK_PATH}"

# 4. 让链接属主与原目录一致（目标目录内容权限保持原样）
chown -h "${OWNER_UID_GID}" "${LINK_PATH}"

echo -e "\n========================================"
echo "✅ 迁移 100% 成功！"
echo "原路径不变：${LINK_PATH}"
echo "实际存储：${DST_DIR}"
echo "========================================"