#!/usr/bin/env bash
set -u

banner() {

cat <<'EOF'
 ██████╗ ██████╗  █████╗ ███╗   ██╗ ██████╗ ███████╗
██╔═══██╗██╔══██╗██╔══██╗████╗  ██║██╔════╝ ██╔════╝
██║   ██║██████╔╝███████║██╔██╗ ██║██║  ███╗█████╗
██║   ██║██╔══██╗██╔══██║██║╚██╗██║██║   ██║██╔══╝
╚██████╔╝██║  ██║██║  ██║██║ ╚████║╚██████╔╝███████╗
 ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═══╝ ╚═════╝ ╚══════╝

██████╗ ██╗    ███████╗███████╗██████╗  ██████╗     ██╗  ██╗
██╔══██╗██║    ╚══███╔╝██╔════╝██╔══██╗██╔═══██╗    ██║  ██║
██████╔╝██║      ███╔╝ █████╗  ██████╔╝██║   ██║    ███████║
██╔═══╝ ██║     ███╔╝  ██╔══╝  ██╔══██╗██║   ██║    ╚════██║
██║     ██║    ███████╗███████╗██║  ██║╚██████╔╝         ██║
╚═╝     ╚═╝    ╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝          ╚═╝

                            ██╗  ██╗
                            ╚██╗██╔╝
                             ╚███╔╝
                             ██╔██╗
                            ██╔╝ ██╗
                            ╚═╝  ╚═╝

███╗   ███╗███████╗███████╗██╗  ██╗ ██████╗ ██████╗ ██████╗ ███████╗
████╗ ████║██╔════╝██╔════╝██║  ██║██╔════╝██╔═══██╗██╔══██╗██╔════╝
██╔████╔██║█████╗  ███████╗███████║██║     ██║   ██║██████╔╝█████╗
██║╚██╔╝██║██╔══╝  ╚════██║██╔══██║██║     ██║   ██║██╔══██╗██╔══╝
██║ ╚═╝ ██║███████╗███████║██║  ██║╚██████╗╚██████╔╝██║  ██║███████╗
╚═╝     ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝ ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚══════╝

                            ██╗  ██╗
                            ╚██╗██╔╝
                             ╚███╔╝
                             ██╔██╗
                            ██╔╝ ██╗
                            ╚═╝  ╚═╝

         ██████╗ ██╗     ██╗      █████╗ ███╗   ███╗ █████╗
        ██╔═══██╗██║     ██║     ██╔══██╗████╗ ████║██╔══██╗
        ██║   ██║██║     ██║     ███████║██╔████╔██║███████║
        ██║   ██║██║     ██║     ██╔══██║██║╚██╔╝██║██╔══██║
        ╚██████╔╝███████╗███████╗██║  ██║██║ ╚═╝ ██║██║  ██║
         ╚═════╝ ╚══════╝╚══════╝╚═╝  ╚═╝╚═╝     ╚═╝╚═╝  ╚═╝
EOF
}

info() {
    local user_host os host kernel uptime_str packages shell_name tty_name cpu_model cpu_count cpu_mhz
    local mem swap disk locale_name iface ip_addr header

    user_host="$(id -un)@$(hostname)"
    header=$(printf '%*s' "${#user_host}" '' | tr ' ' '-')
    os=$( . /etc/os-release 2>/dev/null && printf '%s %s' "${PRETTY_NAME:-Linux}" "$(uname -m)")
    host=
    for f in /sys/firmware/devicetree/base/model /sys/devices/virtual/dmi/id/product_name; do
        if [[ -r "$f" ]]; then host=$(tr -d '\0' < "$f"); break; fi
    done
    kernel="$(uname -s) $(uname -r)"
    uptime_str=$(uptime -p 2>/dev/null | sed 's/^up //')
    if command -v dpkg-query >/dev/null 2>&1; then
        packages="$(dpkg-query -f '.\n' -W 2>/dev/null | wc -l) (dpkg)"
    fi
    shell_name=$(basename "${SHELL:-sh}")
    tty_name=$(tty 2>/dev/null || true)
    cpu_model=$(lscpu 2>/dev/null | awk -F: '/Model name/ { gsub(/^[ \t]+/, "", $2); print $2; exit }')
    cpu_count=$(nproc 2>/dev/null || echo 1)
    cpu_mhz=$(awk '{ printf "%.2f GHz", $1 / 1000000 }' /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq 2>/dev/null || true)
    mem=$(free -m | awk '/^Mem:/ { printf "%.2f MiB / %.2f GiB (%d%%)", $3, $2 / 1024, $3 * 100 / $2 }')
    swap=$(free -m | awk '/^Swap:/ { if ($2 == 0) print "0 B / 0 B"; else printf "%.2f MiB / %.2f GiB (%d%%)", $3, $2 / 1024, $3 * 100 / $2 }')
    disk=$(df -hP / | awk 'NR == 2 { printf "%s / %s (%s)", $3, $2, $5 }')
    disk="$disk - $(findmnt -no FSTYPE / 2>/dev/null)"
    locale_name="${LANG:-C}"
    iface=$(ip -4 route show default 2>/dev/null | awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }')
    ip_addr=$(ip -4 -o addr show dev "$iface" 2>/dev/null | awk '{ print $4; exit }')

    printf '%s\n%s\n' "$user_host" "$header"
    printf 'OS: %s\n' "$os"
    [[ -n "$host" ]] && printf 'Host: %s\n' "$host"
    printf 'Kernel: %s\n' "$kernel"
    printf 'Uptime: %s\n' "$uptime_str"
    [[ -n "${packages:-}" ]] && printf 'Packages: %s\n' "$packages"
    printf 'Shell: %s\n' "$shell_name"
    [[ -n "$tty_name" && "$tty_name" != "not a tty" ]] && printf 'Terminal: %s\n' "$tty_name"
    printf 'CPU: %s (%s)%s\n' "${cpu_model:-unknown}" "$cpu_count" "${cpu_mhz:+ @ $cpu_mhz}"
    printf 'Memory: %s\n' "$mem"
    printf 'Swap: %s\n' "$swap"
    printf 'Disk (/): %s\n' "$disk"
    [[ -n "$ip_addr" ]] && printf 'Local IP (%s): %s\n' "$iface" "$ip_addr"
    printf 'Locale: %s\n' "$locale_name"

if command -v apt >/dev/null 2>&1; then
    upgradable=$(apt list --upgradable 2>/dev/null | awk 'NR > 1 && $0 !~ /^Listing/')
    total=$(printf '%s' "$upgradable" | grep -c . || true)
    security=$(printf '%s' "$upgradable" | grep -ci 'security' || true)
    stamp=/var/lib/apt/periodic/update-success-stamp
    [[ -e "$stamp" ]] || stamp=/var/lib/apt/lists
    last_check=$(date -d "@$(stat -c %Y "$stamp" 2>/dev/null || echo 0)" '+%Y-%m-%d %H:%M')
    printf '\n[ %s security updates available, %s updates total: apt upgrade ]\n' "$security" "$total"
    printf 'Last check: %s\n' "$last_check"
fi
}

gradient() {
    awk -v r1="$1" -v g1="$2" -v b1="$3" -v r2="$4" -v g2="$5" -v b2="$6" -v r3="$7" -v g3="$8" -v b3="$9" '
    { line[NR] = $0 }
    END {
        for (i = 1; i <= NR; i++) {
            t = (NR > 1) ? (i - 1) / (NR - 1) : 0
            if (t < 0.5) { u = t / 0.5; r = r1 + (r2 - r1) * u; g = g1 + (g2 - g1) * u; b = b1 + (b2 - b1) * u }
            else { u = (t - 0.5) / 0.5; r = r2 + (r3 - r2) * u; g = g2 + (g3 - g2) * u; b = b2 + (b3 - b2) * u }
            if (line[i] == "") { print ""; continue }
            printf "\033[38;2;%d;%d;%dm%s\033[0m\n", r, g, b, line[i]
        }
    }'
}

banner | gradient 150 225 255 45 175 215 25 95 150
info | gradient 255 150 150 225 50 50 120 15 15
