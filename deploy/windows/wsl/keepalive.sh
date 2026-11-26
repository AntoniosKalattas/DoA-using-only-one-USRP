#!/bin/bash
# Started hidden by Windows. Keeps Ubuntu (and dockerd) alive: this WSL is too old for systemd.
update-alternatives --set iptables /usr/sbin/iptables-legacy >/dev/null 2>&1
update-alternatives --set ip6tables /usr/sbin/ip6tables-legacy >/dev/null 2>&1
if ! pgrep -x dockerd >/dev/null; then
  nohup dockerd >/var/log/dockerd.log 2>&1 &
fi
exec sleep infinity
