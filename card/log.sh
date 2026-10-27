#!/bin/sh
# log.sh [SECONDS]: for measurements, appends a line to ./load.log every SECONDS (10): the time,
# the battery's charge and status, wlan0's received and sent bytes, the load, and the relay's CPU
# ticks and resident memory, with the free RAM. Start it in the background beside the relay
# (setsid nohup ./log.sh >/dev/null 2>&1 </dev/null &) and stop it with kill.
cd "$(dirname "$0")" || exit 1
every=${1:-10}
bat=/sys/class/power_supply/battery
while :; do
    pid=$(pidof q2-soloify) pid=${pid%% *} cpu= rss=
    net=$(awk '/wlan0:/ { sub(/.*:/, ""); print $1, $9 }' /proc/net/dev)
    if [ -n "$pid" ]; then
        cpu=$(awk '{ print $14 + $15 }' "/proc/$pid/stat" 2>/dev/null)
        rss=$(awk '/^VmRSS/ { print $2 }' "/proc/$pid/status" 2>/dev/null)
    fi
    free=$(awk '/^MemAvailable/ { print $2 }' /proc/meminfo)
    echo "$(date +%T) bat=$(cat $bat/capacity 2>/dev/null) $(cat $bat/status 2>/dev/null)" \
        "wlan_rx_tx=$net load=$(cut -d' ' -f1 /proc/loadavg) cpu_ticks=$cpu rss_kb=$rss" \
        "free_kb=$free" >> ./load.log
    sleep "$every"
done
