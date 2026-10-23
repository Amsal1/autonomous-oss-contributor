# ossbot/proxy (DESIGN §10.1, §11.2): squid 6 from Ubuntu 24.04, no TLS interception.
# Runs as root-managed container `ossbot-proxy` on ossbot-egress (10.212.0.2), no published ports,
# net.ipv4.ip_forward=0 in its netns. The rendered config (ossbot.ctl.render squid) is mounted
# read-only at /etc/squid/squid.conf; access logs go to the mounted /var/log/squid.
FROM ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac

ARG SQUID_VERSION=6.14-0ubuntu0.24.04.5

ENV DEBIAN_FRONTEND=noninteractive LANG=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends "squid=${SQUID_VERSION}" ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && install -d -o proxy -g proxy -m 0750 /var/log/squid /var/spool/squid

EXPOSE 3128 3140 3141 3150
CMD ["/usr/sbin/squid", "-N", "-f", "/etc/squid/squid.conf"]
