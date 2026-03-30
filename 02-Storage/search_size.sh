#!/bin/bash
# search_size.sh - 增强版目录占用扫描：定位大目录、所属用户、所在硬盘，并按 dev+inode 去重

set -euo pipefail # 错误立即退出，未定义变量视为错误，管道失败时返回非零状态

THRESHOLD="5G" # 默认目录大小阈值，单位可为 B/K/M/G/T（如 500M、20G）
MAX_DEPTH=3 # 默认目录扫描深度，0 表示无限制
TOP_N=100 # 默认最多输出条数
CUSTOM_ROOTS=0 # 是否使用自定义扫描根目录
SCAN_ROOTS=("/" "/mnt/data") # 默认扫描目录列表，可通过 --scan-root 参数覆盖

usage() {
    echo "用法: sudo bash ./02-Storage/search_size.sh [选项]"
    echo "  --threshold <SIZE>  目录阈值，默认 5G（如 500M、20G）"
    echo "  --depth <N>         目录深度，默认 3"
    echo "  --top <N>           最多输出条数，默认 100"
    echo "  --scan-root <PATH>  扫描根目录，可重复；默认 /home /mnt/data"
    echo "  -h, --help          查看帮助"
}

human_bytes() {
    # 将字节数转换为人类可读格式（如 KB、MB、GB）
    local bytes="$1"
    if command -v numfmt >/dev/null 2>&1; then
        numfmt --to=iec --suffix=B "$bytes"
    else
        echo "${bytes}B"
    fi
}

size_to_bytes() {
    # 将字节数转换为数字
    local raw="$1"
    local normalized

    normalized=$(echo "$raw" | tr '[:lower:]' '[:upper:]')
    if [[ "$normalized" =~ ^[0-9]+[KMGTPE]?B?$ ]]; then
        if command -v numfmt >/dev/null 2>&1; then
            numfmt --from=iec "$normalized"
            return
        fi

        case "$normalized" in
            *[K] | *KB) echo "$(( ${normalized%%[KkBb]*} * 1024 ))" ;;
            *[M] | *MB) echo "$(( ${normalized%%[MmBb]*} * 1024 * 1024 ))" ;;
            *[G] | *GB) echo "$(( ${normalized%%[GgBb]*} * 1024 * 1024 * 1024 ))" ;;
            *[T] | *TB) echo "$(( ${normalized%%[TtBb]*} * 1024 * 1024 * 1024 * 1024 ))" ;;
            *) echo "${normalized%%B}" ;;
        esac
    else
        echo "0"
    fi
}

resolve_physical_disk() {
    # 获取目录所属的物理硬盘（通过设备路径解析）
    local device="$1"

    if [[ "$device" != /dev/* ]]; then
        echo "N/A"
        return
    fi

    local real_dev disk
    real_dev=$(readlink -f "$device" 2>/dev/null)
    if [ -z "$real_dev" ]; then
        real_dev="$device"
    fi

    disk=$(lsblk -srno NAME,TYPE "$real_dev" 2>/dev/null | awk '$2=="disk" {print "/dev/"$1; exit}')
    if [ -n "$disk" ]; then
        echo "$disk"
    else
        echo "$device"
    fi
}

if ! command -v du >/dev/null 2>&1 || ! command -v lsblk >/dev/null 2>&1 || ! command -v stat >/dev/null 2>&1 || ! command -v df >/dev/null 2>&1; then
    echo "错误: 缺少必要命令(du/lsblk/stat/df)"
    exit 1
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --threshold)
            if [ $# -lt 2 ]; then
                echo "错误: --threshold 需要一个值"
                exit 1
            fi
            THRESHOLD="$2"
            shift 2
            ;;
        --depth)
            if [ $# -lt 2 ]; then
                echo "错误: --depth 需要一个值"
                exit 1
            fi
            MAX_DEPTH="$2"
            shift 2
            ;;
        --top)
            if [ $# -lt 2 ]; then
                echo "错误: --top 需要一个值"
                exit 1
            fi
            TOP_N="$2"
            shift 2
            ;;
        --scan-root)
            if [ $# -lt 2 ]; then
                echo "错误: --scan-root 需要一个路径"
                exit 1
            fi
            if [ "$CUSTOM_ROOTS" -eq 0 ]; then
                SCAN_ROOTS=()
                CUSTOM_ROOTS=1
            fi
            SCAN_ROOTS+=("$2")
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "错误: 不支持的参数 $1"
            usage
            exit 1
            ;;
    esac
done

if ! [[ "$THRESHOLD" =~ ^[0-9]+([kKmMgGtTpPeE])?[bB]?$ ]]; then
    echo "错误: --threshold 格式无效: $THRESHOLD"
    exit 1
fi
if ! [[ "$MAX_DEPTH" =~ ^[0-9]+$ ]]; then
    echo "错误: --depth 必须是非负整数: $MAX_DEPTH"
    exit 1
fi
if ! [[ "$TOP_N" =~ ^[0-9]+$ ]]; then
    echo "错误: --top 必须是正整数: $TOP_N"
    exit 1
fi

if [ "$EUID" -ne 0 ]; then
    echo "警告: 当前不是 root，部分目录可能因权限不足导致统计偏小。建议使用 sudo 运行。"
fi

MIN_BYTES=$(size_to_bytes "$THRESHOLD")
if [ "$MIN_BYTES" -le 0 ]; then
    echo "错误: 无法解析阈值: $THRESHOLD"
    exit 1
fi

RAW_TMP=$(mktemp) # 临时文件
OUT_TMP=$(mktemp) # 存储过滤后的结果，格式: bytes<TAB>dir_path
UNIQ_TMP=$(mktemp) # 存储去重后的结果，格式: bytes<TAB>dir_path
KEY_TMP=$(mktemp)   # 存储 dev+inode 作为键，格式: dev:inode<TAB>bytes<TAB>dir_path，用于去重
trap 'rm -f "$RAW_TMP" "$OUT_TMP" "$UNIQ_TMP" "$KEY_TMP"' EXIT # 脚本结束时清理临时文件

for root in "${SCAN_ROOTS[@]}"; do
    if [ -d "$root" ]; then
        # 某些目录(如 /proc)在扫描过程中可能短暂不可访问，忽略 du 的非零返回码。
        du -B1 --max-depth="$MAX_DEPTH" "$root" 2>/dev/null >> "$RAW_TMP" || true
    fi
done

sort -nr "$RAW_TMP" | awk -v min="$MIN_BYTES" '$1 >= min' > "$OUT_TMP"

# 同一个目录可能通过多个挂载入口出现，按 dev+inode 去重。
while IFS=$'\t' read -r bytes dir_path; do
    key=$(stat -c '%d:%i' "$dir_path" 2>/dev/null || echo "ERR:$dir_path")
    echo -e "$key\t$bytes\t$dir_path"
done < "$OUT_TMP" > "$KEY_TMP"

awk -F'\t' -v top_n="$TOP_N" '!seen[$1]++ {print $2 "\t" $3; if (++count >= top_n) exit}' "$KEY_TMP" > "$UNIQ_TMP"
mv "$UNIQ_TMP" "$OUT_TMP"

if [ ! -s "$OUT_TMP" ]; then
    echo "未找到超过阈值的大目录。"
    exit 0
fi

echo "===== 大目录扫描（增强版）====="
echo "时间: $(date '+%Y-%m-%d %H:%M:%S')"
echo "阈值: $THRESHOLD"
echo "深度: $MAX_DEPTH"
echo "最多输出: $TOP_N"
echo "扫描根目录: ${SCAN_ROOTS[*]}"

printf '%-10s | %-12s | %-18s | %-18s | %-18s | %s\n' "大小" "OWNER" "逻辑分区" "挂载点" "物理硬盘" "目录路径"
printf -- '%.0s-' {1..140}
echo

while IFS=$'\t' read -r bytes dir_path; do
    owner=$(stat -c %U "$dir_path" 2>/dev/null || echo "N/A")
    fs_dev=$(df -P "$dir_path" 2>/dev/null | awk 'NR==2 {print $1}')
    mount_point=$(df -P "$dir_path" 2>/dev/null | awk 'NR==2 {print $NF}')
    phy_disk=$(resolve_physical_disk "$fs_dev")
    size_h=$(human_bytes "$bytes")

    printf '%-10s | %-12s | %-18s | %-18s | %-18s | %s\n' "$size_h" "$owner" "$fs_dev" "$mount_point" "$phy_disk" "$dir_path"
done < "$OUT_TMP"
