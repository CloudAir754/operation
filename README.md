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