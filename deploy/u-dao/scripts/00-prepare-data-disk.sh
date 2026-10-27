#!/usr/bin/env bash
# U-Dao 数据盘初始化：GPT 单分区 + ext4(label=u-dao, reserved 1%) + fstab(UUID) + 挂载 /srv/u-dao。
# 仅限全新空盘 /dev/vdb；禁止操作 /dev/vda。任何事实不符 → DATA_DISK_PRECHECK=FAIL 立即停止。
# 用法：sudo bash deploy/u-dao/scripts/00-prepare-data-disk.sh
set -euo pipefail

DISK=/dev/vdb
TARGET=/srv/u-dao
EXPECTED_MIN_BYTES=200000000000 # 200GiB ≈ 214748364800 bytes

fail() { echo "DATA_DISK_PRECHECK=FAIL: $1" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "must run as root (sudo)"
for cmd in parted mkfs.ext4 wipefs tune2fs blkid findmnt; do
  command -v "$cmd" >/dev/null 2>&1 || fail "missing command: $cmd"
done

# ---- 0) 系统盘保护：/ 必须位于 vda ----
root_src="$(findmnt -n -o SOURCE /)" || fail "cannot resolve / source"
case "$root_src" in
  /dev/vda*|/dev/disk/by-*/*vda*) : ;;
  *) fail "unexpected root device: $root_src（预期 vda 系）" ;;
esac
echo "root_device=$root_src（未触碰）"

# ---- 1) device size ≈ 200GiB ----
bytes="$(lsblk -b -n -o SIZE "$DISK")" || fail "cannot read $DISK size"
[ "$bytes" -ge "$EXPECTED_MIN_BYTES" ] || fail "$DISK size=$bytes 低于预期 $EXPECTED_MIN_BYTES"
echo "size_bytes=$bytes"

# ---- 2) 未挂载 / 无 mountpoint ----
mnt="$(lsblk -n -o MOUNTPOINTS "$DISK" | tr -d ' ')"
[ -z "$mnt" ] || fail "$DISK 已挂载于: $mnt"
echo "mountpoints=无"

# ---- 3) 无 holders（不被 dm/raid/lvm 等使用）。
# 注意：仅用 sysfs 检查；lsblk 的 HOLDERS 列需 util-linux>=2.40，Ubuntu 22.04(2.37) 不支持。----
holder_count="$(find "/sys/block/vdb/holders" -mindepth 1 -maxdepth 1 2>/dev/null | wc -l)"
[ "$holder_count" -eq 0 ] || fail "$DISK 存在 $holder_count 个 holders"
echo "holders=无"

# ---- 4) 无文件系统签名 ----
sig="$(wipefs -n "$DISK" 2>/dev/null || true)"
[ -z "$sig" ] || fail "$DISK 已存在文件系统签名: $sig"
echo "filesystem_signatures=无"

# ---- 5) 无分区 ----
parts="$(lsblk -n -o NAME "$DISK" | tail -n +2 | tr -d ' ')"
[ -z "$parts" ] || fail "$DISK 已存在分区: $parts"
echo "partitions=无"

echo "DATA_DISK_PRECHECK=PASS"

# ---- 创建 GPT 单分区 ----
echo "== 创建 GPT 单分区 $DISK → ${DISK}1 =="
parted -s "$DISK" mklabel gpt
parted -s "$DISK" mkpart u-dao ext4 0% 100%
partprobe "$DISK" 2>/dev/null || true
sleep 2
PART="${DISK}1"
[ -b "$PART" ] || fail "分区 $PART 未出现"

# ---- 格式化 ext4（label=u-dao；纯数据盘 reserved blocks 1%）----
echo "== 格式化 ext4（label=u-dao）=="
mkfs.ext4 -q -L u-dao "$PART"
tune2fs -m 1 "$PART" >/dev/null
tune2fs -l "$PART" | grep -E "Filesystem volume name|Reserved block count"

# ---- UUID 二次核对并写 fstab ----
UUID="$(blkid -s UUID -o value "$PART")"
[ -n "$UUID" ] || fail "blkid 未取得 UUID"
label="$(blkid -s LABEL -o value "$PART")"
[ "$label" = "u-dao" ] || fail "label 异常: $label"
echo "uuid=$UUID label=$label"
if grep -q "^UUID=$UUID" /etc/fstab; then
  echo "fstab 已包含 UUID=$UUID"
elif grep -q "[[:space:]]$TARGET[[:space:]]" /etc/fstab; then
  fail "fstab 已存在 $TARGET 条目但 UUID 不同，人工确认后再操作"
else
  cp /etc/fstab "/etc/fstab.bak.$(date +%Y%m%dT%H%M%SZ)"
  printf 'UUID=%s %s ext4 defaults,nofail,x-systemd.device-timeout=10 0 2\n' "$UUID" "$TARGET" >> /etc/fstab
  echo "fstab 已写入（备份于 /etc/fstab.bak.*）"
fi

# ---- 挂载与验证 ----
echo "== 挂载 $TARGET =="
mkdir -p "$TARGET"
mount "$TARGET" || fail "mount 失败"
mount -a
findmnt "$TARGET" >/dev/null || fail "findmnt 校验失败"
findmnt -o TARGET,SOURCE,FSTYPE,SIZE "$TARGET"
df -hT "$TARGET" | tail -1

# ---- 目录骨架（业务文件 ubuntu:ubuntu ownership；docker/containerd 数据目录交由 daemon）----
echo "== 建立目录骨架 =="
mkdir -p "$TARGET"/platform/{ingress/conf.d,docker,containerd,monitoring,ops} \
         "$TARGET"/projects/firebot/{repo,releases,runtime/{env,secrets,certs,deployments},backups,logs,docs} \
         "$TARGET"/projects/crane-platform \
         "$TARGET"/backups \
         "$TARGET"/docs
chown -R ubuntu:ubuntu "$TARGET"

# ---- 软链（登录便利；canonical 路径仍是 /srv/u-dao）----
ln -sfn "$TARGET" /home/ubuntu/u-dao
echo "symlink=/home/ubuntu/u-dao -> $TARGET"

echo "DATA_DISK_INIT=PASS label=u-dao uuid=$UUID"
