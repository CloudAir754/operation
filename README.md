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

# 2. 存储维护脚本
`./store/`文件夹
