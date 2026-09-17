# 静态资源仓 FTP 服务（2026-09-17）：镜像源白名单只放基础镜像，
# stilliard/pure-ftpd、fauria/vsftpd 均被拦，故用已缓存的 ubuntu:24.04
# 自建（与 test-runner/bi-web 同基座，构建可复用其 apt 缓存层）。
FROM ubuntu:24.04

RUN apt-get update \
    && apt-get install --yes --no-install-recommends vsftpd \
    && rm -rf /var/lib/apt/lists/*

COPY docker/integration/ftp/vsftpd.conf /etc/vsftpd.conf
COPY docker/integration/ftp/entrypoint.sh /entrypoint.sh
# 宿主编辑可能带 CRLF（Windows）：vsftpd 会把 'YES\r' 判成 bad bool，
# 构建期统一去 CR，比依赖宿主行尾约定稳。
RUN sed -i 's/\r$//' /etc/vsftpd.conf /entrypoint.sh \
    && chmod 0755 /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
