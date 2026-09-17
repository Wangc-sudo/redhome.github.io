#!/usr/bin/env bash
# 启动时按环境变量建/更新 FTP 账号并接管共享卷属主，然后前台拉起 vsftpd。
set -euo pipefail

: "${FTP_USER_NAME:?set FTP_USER_NAME}"
: "${FTP_USER_PASS:?set FTP_USER_PASS}"

id "$FTP_USER_NAME" >/dev/null 2>&1 || \
    useradd --home /home/bistatic --shell /usr/sbin/nologin "$FTP_USER_NAME"
echo "$FTP_USER_NAME:$FTP_USER_PASS" | chpasswd

# Debian/Ubuntu 的 /etc/pam.d/vsftpd 带 pam_shells：nologin 不在
# /etc/shells 会被 PAM 判 530（与 vsftpd 的 check_shell=NO 是两层）。
grep -qxF '/usr/sbin/nologin' /etc/shells || echo '/usr/sbin/nologin' >> /etc/shells

mkdir -p /home/bistatic/files
chown -R "$FTP_USER_NAME:$FTP_USER_NAME" /home/bistatic

# vsftpd 的 secure_chroot_dir（默认 /var/run/vsftpd/empty）在 tmpfs 上，
# 每次启动都要重建，否则客户端连上即被 500 拒绝。
mkdir -p /var/run/vsftpd/empty

exec /usr/sbin/vsftpd /etc/vsftpd.conf
