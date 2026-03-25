#!/bin/bash
# 迁移脚本：从旧目录迁移到新目录，带空间检查、rsync传输、权限修改等步骤
set -e

# ====== 固定配置 ======
SRC_DIR="/data/old"
DST_DIR="/data/new"
OWNER="myuser"
LOG_FILE="./migrate.log"
# =====================

echo "====== 开始迁移 ======"

# 检查源目录
if [ ! -d "$SRC_DIR" ]; then
  echo "错误: 源目录不存在"
  exit 1
fi

mkdir -p "$DST_DIR"

# ====== 1️⃣ 检查源目录大小 ======
echo "计算源目录大小..."
SRC_SIZE=$(du -sb "$SRC_DIR" | awk '{print $1}')
echo "源目录大小: $SRC_SIZE bytes"

# ====== 2️⃣ 检查目标磁盘剩余空间 ======
echo "检查目标磁盘剩余空间..."
AVAILABLE=$(df -B1 "$DST_DIR" | awk 'NR==2 {print $4}')
echo "目标剩余空间: $AVAILABLE bytes"

if [ "$AVAILABLE" -lt "$SRC_SIZE" ]; then
  echo "❌ 空间不足！停止迁移"
  exit 1
else
  echo "✅ 空间充足，开始迁移"
fi

# ====== 3️⃣ rsync迁移（带进度 + 校验）======
echo "开始 rsync 传输..."

rsync -a --info=progress2 --delete \
  "$SRC_DIR"/ "$DST_DIR"/ | tee "$LOG_FILE"

echo "首次同步完成"

# ====== 4️⃣ 二次校验（强校验）======
echo "开始校验（checksum）..."

rsync -a -c --delete \
  "$SRC_DIR"/ "$DST_DIR"/

echo "✅ rsync 校验通过"

# ====== 5️⃣ 修改权限 ======
echo "修改权限..."
chown -R "$OWNER":"$OWNER" "$DST_DIR"

# ====== 6️⃣ 删除源数据（谨慎）======
echo "请自行！！！！删除源目录内容..."
# rm -rf "$SRC_DIR"/*

echo "====== 迁移完成 ✅ ======"