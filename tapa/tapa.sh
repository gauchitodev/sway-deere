#!/bin/sh
# Modo "tapa cerrada": al cerrar la tapa se bloquea y se apaga la pantalla,
# pero la compu sigue andando (descargas, música, SSH por Tailscale...).
# Es momentáneo: se apaga con el mismo botón, o solo al reiniciar o cerrar sesión.
#   tapa.sh          → prende o apaga el modo (botón de la barra y Super+Shift+T)
#   tapa.sh estado   → lo que muestra la barra (JSON de waybar)
#   tapa.sh cerrada  → lo llama sway al cerrar la tapa
#   tapa.sh abierta  → lo llama sway al abrir la tapa
#
# Funciona con un "bloqueo" de systemd (handle-lid-switch): mientras está
# tomado, logind no suspende al cerrar la tapa. No toca ninguna config del sistema.
POR_QUE='G5: tapa cerrada sin suspender'
PANTALLA='eDP-1'

activo() { pgrep -u "$(id -u)" -f "^systemd-inhibit --what=handle-lid-switch --who=G5" >/dev/null; }
avisar_barra() { pkill -RTMIN+10 -x waybar 2>/dev/null; }

case "$1" in
    estado)
        if activo; then
            printf '{"text":"  Tapa","class":"on","tooltip":"Tapa cerrada: se bloquea y apaga la pantalla, pero NO se suspende.\\nNo la guardes así en la mochila: se calienta y gasta batería.\\nTocá para volver a lo normal."}\n'
        else
            printf '{"text":"","class":"off","tooltip":"Tocá para que al cerrar la tapa la compu siga andando (sin suspender)"}\n'
        fi
        ;;
    cerrada)
        # Sin el modo, logind suspende solo (y swayidle bloquea antes de dormir)
        if activo; then
            pgrep -u "$(id -u)" -x swaylock >/dev/null || swaylock -f
            swaymsg -q "output $PANTALLA power off"
        fi
        ;;
    abierta)
        swaymsg -q "output $PANTALLA power on"
        ;;
    *)
        if activo; then
            pkill -u "$(id -u)" -f "^systemd-inhibit --what=handle-lid-switch --who=G5"
            notify-send -a G5 "Tapa: normal" "Al cerrar la tapa se suspende, como siempre."
        else
            setsid systemd-inhibit --what=handle-lid-switch --who=G5 --why="$POR_QUE" --mode=block \
                sleep infinity >/dev/null 2>&1 < /dev/null &
            sleep 0.3
            if activo; then
                notify-send -a G5 "Tapa: sigue andando" "Al cerrar la tapa se bloquea y apaga la pantalla, sin suspender. Tocá de nuevo para volver a lo normal."
            else
                notify-send -a G5 -u critical "Tapa" "No se pudo activar el modo."
            fi
        fi
        avisar_barra
        ;;
esac
