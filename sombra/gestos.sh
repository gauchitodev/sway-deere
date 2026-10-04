#!/bin/sh
# Gestos de la pantalla táctil: deslizar desde el borde de arriba baja el panel de estado,
# deslizar hacia arriba lo cierra. Necesita lisgd.
NOMBRE=$(swaymsg -t get_inputs | jq -r '[.[] | select(.type=="touch")][0].name // empty')
[ -n "$NOMBRE" ] || exit 0
EV=$(grep -lxF "$NOMBRE" /sys/class/input/event*/device/name | head -n1 | cut -d/ -f5)
[ -n "$EV" ] || exit 0
pkill -u "$(id -u)" -x lisgd
exec lisgd -d "/dev/input/$EV" \
    -g "1,UD,T,*,R,$HOME/.config/g5/sombra/abrir.sh abrir" \
    -g "1,DU,*,*,R,$HOME/.config/g5/sombra/abrir.sh cerrar"
