#!/bin/bash
set -euo pipefail

# ===================== 配置项 =====================
USER="" # 请替换为实际用户名，必须与用户目录中的用户名一致
SRC_DIR="" # 源目录
DST_BASE="" # 目标基目录，最终路径会是 ${DST_BASE}/xxxxx
DST_DIR="${DST_BASE}/xxxxx" # 目标完整路径
LINK_PATH="${SRC_DIR}"
# ==================================================

# 检查是否以 root 运行
if [ "$(id -u)" -ne 0 ]; then
    echo "❌ 错误：必须使用 sudo 运行此脚本！"
    exit 1
fi

# 检查源目录是否存在
if [ ! -d "${SRC_DIR}" ]; then
    echo "❌ 错误：源目录不存在 → ${SRC_DIR}"
    exit 1
fi

# 检查目标目录不能已存在
if [ -d "${DST_DIR}" ]; then
    echo "❌ 错误：目标目录已存在，避免覆盖 → ${DST_DIR}"
    exit 1
fi

# 检查软链接不能已存在
if [ -L "${LINK_PATH}" ] || [ -e "${LINK_PATH}" ]; then
    echo "❌ 错误：链接路径已存在，无法创建 → ${LINK_PATH}"
    exit 1
fi

echo "✅ 所有检查通过，开始迁移..."

# 1. 创建目标目录
mkdir -p "${DST_BASE}"

# 2. 移动目录
echo "→ 移动：${SRC_DIR} → ${DST_BASE}"
mv "${SRC_DIR}" "${DST_BASE}/"

# 3. 创建软链接
echo "→ 创建软链接：${LINK_PATH} -> ${DST_DIR}"
ln -s "${DST_DIR}" "${LINK_PATH}"

# 4. 修复所有权限（关键！）
chown -h "${USER}:${USER}" "${LINK_PATH}"
chown -R "${USER}:${USER}" "${DST_DIR}"

echo -e "\n========================================"
echo "✅ 迁移 100% 成功！"
echo "原路径不变：${LINK_PATH}"
echo "实际存储：${DST_DIR}"
echo "========================================"