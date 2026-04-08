# 常用运维脚本

# 1. 用户维护脚本
`./01-UserControl/`文件夹

## 1.1 批量创建用户并生成随机密码
- 修改`./01-UserControl/user_list.txt`，格式为 `username RealName` 行末需要换行！
- `bash ./01-UserControl//create_users_v2.sh` 该脚本用于创建用户
- 生成的密码文件在 `./01-UserControl/user_passwords.log`

## 1.2 删除用户并释放删除文件
- 修改`01-UserControl/clean_user.sh` 中 `USER` 变量
- `bash ./01-UserControl/clean_user.sh`

## 1.3 列出用户，及其占用大小和位置
- `bash ./01-UserControl/whole_user.sh` 默认仅显示普通可登录用户
- `bash ./01-UserControl/whole_user.sh --all` 显示全部账户（含系统账户）

# 2. 存储维护脚本
`./02-Storage/`文件夹

## 2.1 移动用户整体到另外的磁盘，并用mount 挂载，保证/home/username正常
- 修改`02-Storage/move_home2.sh`中的 `USERNAME`
- `bash ./02-Storage/move_home2.sh`

## 2.2 移动部分文件，保证权限不变
- `bash move_project_to_mnt.sh`

## 2.3 移动修正，主要是针对v1的移动脚本，丢失/home/username 的情况
- `move_home_Repairblind.sh`

## 2.4 搜索用户占磁盘空间脚本
- `02-Storage/user_disk_usage.sh`

## 2.5 迁移某文件夹，恢复权限
- `02-Storage/migrate.sh`

## 2.6 扫描大目录占用（增强版）
- 脚本：`02-Storage/search_size.sh`
- 作用：扫描大目录，显示目录大小、目录所有者、逻辑分区、挂载点、物理硬盘。
- 特性：按 `dev+inode` 去重，避免同一目录通过多个挂载入口重复显示。
- 说明：脚本在扫描阶段会忽略少量临时不可访问目录（如 `/proc`）导致的 `du` 非零返回码，不会因此提前退出。

### 用法
- `sudo bash ./02-Storage/search_size.sh`
- `sudo bash ./02-Storage/search_size.sh --threshold 100G --depth 3 --top 50`
- `sudo bash ./02-Storage/search_size.sh --scan-root /home --scan-root /mnt/data --threshold 20G --depth 4 --top 100`

### 参数
- `--threshold <SIZE>`：目录阈值，默认 `5G`（如 `500M`、`20G`）
- `--depth <N>`：扫描深度，默认 `3`
- `--top <N>`：最多输出条数，默认 `100`
- `--scan-root <PATH>`：扫描根目录，可重复；默认 `/ /mnt/data`
- `-h, --help`：查看帮助

### 常见问题
- 现象：执行后看起来“没有输出”。
- 排查：
	- 先用更小范围验证：`sudo bash ./02-Storage/search_size.sh --scan-root /home --depth 2 --threshold 1G --top 20`
	- 若目录都小于阈值，脚本会输出 `未找到超过阈值的大目录。`
	- 建议优先指定 `--scan-root`，避免全盘扫描耗时较长时误以为卡住。

## 2.7 迁移 Docker 数据目录到 /mnt/data/docker（Bind Mount）
- 脚本：`02-Storage/move_docker_to_mnt.sh`
- 默认迁移：`/var/lib/docker -> /mnt/data/docker`
- 特性：自动检查空间、停止/恢复 Docker 服务、`rsync` 迁移、生成备份、写入 `/etc/fstab` 持久化挂载。

### 用法
- 先检查 Docker 数据量：`sudo du -sh /var/lib/docker`
- 执行迁移：`sudo bash ./02-Storage/move_docker_to_mnt.sh`

### 迁移后检查
- 检查挂载是否生效：`mount | grep '/var/lib/docker'`
- 检查服务状态：`sudo systemctl status docker --no-pager`
- 检查容器列表：`docker ps -a`

### 备注
- 脚本会将原目录备份为：`/var/lib/docker.bak.时间戳`
- 确认业务运行正常后，再手动删除备份目录。