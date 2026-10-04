#!/bin/sh
# Panel de estado G5: se baja desde la barra de arriba, como las notificaciones del teléfono.
# Lo sirve el mismo servidor de la pantalla de inicio (en /sombra).
# Si ya está abierto lo cierra (tocar la barra otra vez, o deslizar hacia arriba).
APP='chrome-localhost__sombra-Default'
YO=$(id -u)
# Mismo cálculo que server.py: un puerto por usuario
PUERTO="${G5_PUERTO:-$((8765 + (YO > 1000 ? YO - 1000 : 0) % 1000))}"

existe=$(swaymsg -t get_tree | jq --arg a "$APP" '[.. | objects | select((.app_id // "") == $a)] | length')
if [ "$existe" -gt 0 ]; then
    swaymsg "[app_id=\"$APP\"] kill" >/dev/null
    exit 0
fi

pgrep -u "$YO" -f "^python3 $HOME/.config/g5/home/server.py" >/dev/null || {
    "$HOME/.config/g5/home/arrancar.sh" >/dev/null 2>&1 &
    sleep 0.5
}

exec chromium \
    --user-data-dir="$HOME/.local/share/g5/sombra" \
    --app="http://localhost:$PUERTO/sombra" \
    --no-first-run --no-default-browser-check \
    --ozone-platform=wayland \
    --disable-features=Translate --disable-pinch --overscroll-history-navigation=0 \
    >/dev/null 2>&1
