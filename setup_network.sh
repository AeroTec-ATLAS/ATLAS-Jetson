#!/usr/bin/env bash
set -u

BRIDGE_CONN="atlas-siyi"

A8_MAC="9c:69:d3:22:a1:a1"
A8_CONN="atlas-siyi-eth2"

HM30_MAC="00:e0:4c:25:72:a0"
HM30_CONN="atlas-siyi-eth1"

A8_IP="192.168.144.25"
GROUND_IP="192.168.144.12"
JETSON_IP="192.168.144.100"

GIMBAL_PORT="37256"

find_iface_by_mac() {
    local wanted_mac="$1"
    local path dev mac

    for path in /sys/class/net/*/address; do
        [[ -r "$path" ]] || continue

        dev="$(basename "$(dirname "$path")")"

        # Ignorar interfaces virtuais como br0/docker0/etc.
        [[ -e "/sys/class/net/$dev/device" ]] || continue

        mac="$(tr '[:upper:]' '[:lower:]' < "$path")"

        if [[ "$mac" == "$wanted_mac" ]]; then
            echo "$dev"
            return 0
        fi
    done

    return 1
}

wait_iface() {
    local mac="$1"
    local label="$2"
    local iface=""

    # IMPORTANTE: mensagens para stderr para não contaminarem $(...)
    echo "[WAIT] À procura da interface física de ${label} (${mac})..." >&2

    for _ in $(seq 1 60); do
        if iface="$(find_iface_by_mac "$mac")"; then
            echo "$iface"
            return 0
        fi
        sleep 1
    done

    return 1
}

carrier() {
    cat "/sys/class/net/$1/carrier" 2>/dev/null || echo 0
}

wait_carrier() {
    local iface="$1"
    local label="$2"

    echo "[WAIT] À espera de carrier em ${label} (${iface})..."

    while [[ "$(carrier "$iface")" != "1" ]]; do
        sleep 1
    done

    echo "[ OK ] ${label}: carrier detetado."
}

bring_up_profile() {
    local conn="$1"

    echo "[NET ] A ativar perfil ${conn}..."

    until sudo nmcli con up "$conn" >/dev/null 2>&1; do
        sleep 2
    done

    echo "[ OK ] ${conn} ativo."
}

wait_ping() {
    local ip="$1"
    local label="$2"

    echo "[WAIT] À espera de ${label} (${ip})..."

    until ping -c 1 -W 1 "$ip" >/dev/null 2>&1; do
        sleep 1
    done

    echo "[ OK ] ${label} responde."
}

echo
echo "============================================================"
echo " ATLAS - PREPARAR REDE + HM30 + CONTROLO GIMBAL"
echo "============================================================"
echo

echo "[NET ] A garantir bridge..."
until sudo nmcli con up "$BRIDGE_CONN" >/dev/null 2>&1; do
    sleep 2
done
echo "[ OK ] Bridge ativa."

A8_IF="$(wait_iface "$A8_MAC" "A8")" || {
    echo "[ERRO] Não encontrei a interface física da A8."
    exit 1
}

HM30_IF="$(wait_iface "$HM30_MAC" "HM30")" || {
    echo "[ERRO] Não encontrei a interface física da HM30."
    exit 1
}

echo
echo "[INFO] Interfaces detetadas neste boot:"
echo "       A8   -> ${A8_IF} (${A8_MAC})"
echo "       HM30 -> ${HM30_IF} (${HM30_MAC})"
echo

# A8
sudo ip link set "$A8_IF" up
bring_up_profile "$A8_CONN"
wait_carrier "$A8_IF" "A8"

# HM30
sudo ip link set "$HM30_IF" up
sudo ethtool -s "$HM30_IF" speed 10 duplex full autoneg off
bring_up_profile "$HM30_CONN"
sudo ethtool -s "$HM30_IF" speed 10 duplex full autoneg off
wait_carrier "$HM30_IF" "HM30 Air"

echo
echo "[INFO] Estado Ethernet:"
echo "=== A8 / ${A8_IF} ==="
sudo ethtool "$A8_IF" | grep -E "Speed|Duplex|Auto-negotiation|Link detected" || true

echo
echo "=== HM30 / ${HM30_IF} ==="
sudo ethtool "$HM30_IF" | grep -E "Speed|Duplex|Auto-negotiation|Link detected" || true

echo
echo "[INFO] Bridge:"
bridge link | grep -E "${A8_IF}|${HM30_IF}" || true
echo

wait_ping "$A8_IP" "A8 Mini"

if ping -c 1 -W 1 "$GROUND_IP" >/dev/null 2>&1; then
    echo "[ OK ] HM30 Ground (${GROUND_IP}) responde."
else
    echo "[INFO] HM30 Ground (${GROUND_IP}) não responde neste momento."
fi

if ss -ltn 2>/dev/null | grep -q ":${GIMBAL_PORT} "; then
    echo "[INFO] Proxy da gimbal já está ativo na porta ${GIMBAL_PORT}."
else
    echo "[CTRL] A iniciar proxy ${JETSON_IP}:${GIMBAL_PORT} -> ${A8_IP}:${GIMBAL_PORT}"

    nohup socat \
        TCP-LISTEN:${GIMBAL_PORT},bind=${JETSON_IP},reuseaddr,fork \
        TCP:${A8_IP}:${GIMBAL_PORT} \
        >/tmp/atlas_gimbal_socat.log 2>&1 &

    sleep 1

    if ss -ltn 2>/dev/null | grep -q ":${GIMBAL_PORT} "; then
        echo "[ OK ] Proxy da gimbal ativo."
    else
        echo "[ERRO] O proxy da gimbal não ficou ativo."
        exit 1
    fi
fi

echo
echo "============================================================"
echo " REDE PRONTA"
echo "============================================================"
echo
echo "Agora executa manualmente:"
echo
echo "cd /home/atlas/Desktop/ATLAS-Jetson/scripts"
echo "MODEL_PATH=/home/atlas/Desktop/ATLAS-Jetson/yolo11s.pt python3 atlas_vision_local.py"
echo
