#!/bin/sh
# Panel de estado G5: se baja desde la barra de arriba, como las notificaciones del teléfono.
# Lo sirve el mismo servidor de la pantalla de inicio (en /sombra).
# La ventana queda precargada y escondida (scratchpad) para que aparezca al instante;
# la animación de bajar y subir la hace sway moviendo la ventana.
#   abrir.sh           alterna      abrir.sh abrir / cerrar     abrir.sh precargar (la deja lista)
APP='chrome-localhost__sombra-Default'
YO=$(id -u)
ALTO=330
PASOS=10
# Mismo cálculo que server.py: un puerto por usuario
PUERTO="${G5_PUERTO:-$((8765 + (YO > 1000 ? YO - 1000 : 0) % 1000))}"

exec 9>"/tmp/g5-sombra-$YO.lock"
flock 9

estado() {
    swaymsg -t get_tree | jq -r --arg a "$APP" '[.. | objects | select((.app_id // "") == $a)]
        | if length == 0 then "no" elif .[0].visible then "visible" else "oculta" end'
}

precargar() {
    [ "$(estado)" != no ] && return 0
    pgrep -u "$YO" -f "^python3 $HOME/.config/g5/home/server.py" >/dev/null || {
        setsid "$HOME/.config/g5/home/arrancar.sh" >/dev/null 2>&1 9>&- &
        sleep 0.5
    }
    chromium \
        --user-data-dir="$HOME/.local/share/g5/sombra" \
        --app="http://localhost:$PUERTO/sombra" \
        --no-first-run --no-default-browser-check \
        --ozone-platform=wayland \
        --disable-features=Translate --disable-pinch --overscroll-history-navigation=0 \
        >/dev/null 2>&1 9>&- &
    # Al aparecer, sway la manda sola al scratchpad (regla en sway.conf)
    i=0
    while [ "$(estado)" = no ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i + 1)); done
    sleep 0.3
}

# Posiciones de la animación: frenan suave al final (cúbica)
tramo() { # $1 = bajar | subir
    awk -v n=$PASOS -v h=$ALTO -v d="$1" 'BEGIN { for (i = 1; i <= n; i++) {
        t = i / n; e = 1 - (1 - t) ^ 3
        printf "%d\n", (d == "bajar") ? -h + h * e : -h * e } }'
}

mover() { swaymsg "[app_id=\"$APP\"] move position 0 px $1 px" >/dev/null; }

abrir() {
    precargar
    [ "$(estado)" = visible ] && return 0
    swaymsg "[app_id=\"$APP\"] scratchpad show, resize set width 100 ppt height $ALTO px, move position 0 px -$ALTO px" >/dev/null
    for y in $(tramo bajar); do mover "$y"; sleep 0.010; done
}

cerrar() {
    [ "$(estado)" = visible ] || return 0
    for y in $(tramo subir); do mover "$y"; sleep 0.010; done
    swaymsg "[app_id=\"$APP\"] move scratchpad" >/dev/null
}

case "$1" in
    precargar) precargar ;;
    abrir) abrir ;;
    cerrar) cerrar ;;
    *) if [ "$(estado)" = visible ]; then cerrar; else abrir; fi ;;
esac
