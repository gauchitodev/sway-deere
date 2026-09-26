#!/bin/sh
# Ventana de wifi G5: redes cerca, conectarse y poner la contraseña.
# La sirve el mismo servidor de la pantalla de inicio (en /wifi).
# Si ya está abierta la trae al frente, y si ya estaba al frente la cierra.
APP='chrome-localhost__wifi-Default'
YO=$(id -u)
# Mismo cálculo que server.py: un puerto por usuario
PUERTO="${G5_PUERTO:-$((8765 + (YO > 1000 ? YO - 1000 : 0) % 1000))}"

enfocada=$(swaymsg -t get_tree | jq -r '.. | objects | select(.focused == true) | .app_id // ""')
existe=$(swaymsg -t get_tree | jq --arg a "$APP" '[.. | objects | select((.app_id // "") == $a)] | length')

if [ "$existe" -gt 0 ]; then
    if [ "$enfocada" = "$APP" ]; then
        swaymsg "[app_id=\"$APP\"] kill" >/dev/null
    else
        swaymsg "[app_id=\"$APP\"] focus" >/dev/null
    fi
    exit 0
fi

pgrep -u "$YO" -f "^python3 $HOME/.config/g5/home/server.py" >/dev/null || {
    "$HOME/.config/g5/home/arrancar.sh" >/dev/null 2>&1 &
    sleep 0.5
}

# "localhost" y no 127.0.0.1: así sway no la confunde con la pantalla de inicio
exec chromium \
    --user-data-dir="$HOME/.local/share/g5/wifi" \
    --app="http://localhost:$PUERTO/wifi" \
    --no-first-run --no-default-browser-check \
    --disable-features=Translate --disable-pinch \
    >/dev/null 2>&1
