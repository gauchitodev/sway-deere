#!/bin/sh
# Gestos de la pantalla táctil: deslizar desde el borde de arriba baja el panel de estado,
# deslizar hacia arriba lo cierra; el panel sigue al dedo (lo hace arrastre.py).
"$HOME/.config/g5/sombra/abrir.sh" precargar >/dev/null 2>&1 &
NOMBRE=$(swaymsg -t get_inputs | jq -r '[.[] | select(.type=="touch")][0].name // empty')
[ -n "$NOMBRE" ] || exit 0
EV=$(grep -lxF "$NOMBRE" /sys/class/input/event*/device/name | head -n1 | cut -d/ -f5)
[ -n "$EV" ] || exit 0
pkill -u "$(id -u)" -x lisgd
pkill -u "$(id -u)" -f "^python3 .*sombra/arrastre.py"
exec python3 "$HOME/.config/g5/sombra/arrastre.py" "/dev/input/$EV"
