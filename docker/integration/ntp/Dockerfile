# dops-ntp：chronyd 授时容器
# 上游公网 NTP（百度/清华 TUNA），下游对私网网段提供 123/udp 授时。
# 运行时必须 host 网络 + SYS_TIME 能力（见 docker-compose.ntp-cloud.yml），
# chronyd 直接校准宿主机内核时钟，宿主机上全部容器共享该时钟自动受益。
FROM alpine:3.20

RUN apk add --no-cache chrony

COPY chrony.conf /etc/chrony/chrony.conf

EXPOSE 123/udp

# -n 前台运行（compose 管生命周期）；不随便放弃特权，授时精度由容器隔离保证
ENTRYPOINT ["chronyd", "-n", "-f", "/etc/chrony/chrony.conf"]
