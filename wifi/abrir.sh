#!/bin/sh
# Ventana de wifi: redes cerca, conectarse y poner contraseña (iwgtk, usa iwd).
# Si ya está abierta la trae al frente, y si ya estaba al frente la cierra.
# Si iwgtk no está instalado, cae a iwctl en una terminal.
APP='org.twosheds.iwgtk'

if ! command -v iwgtk >/dev/null 2>&1; then
    exec foot -T Wifi -e iwctl
fi

enfocada=$(swaymsg -t get_tree | jq -r '.. | objects | select(.focused == true) | .app_id // ""')
existe=$(swaymsg -t get_tree | jq --arg a "$APP" '[.. | objects | select((.app_id // "") == $a and .pid != null)] | length')

if [ "$existe" -gt 0 ]; then
    if [ "$enfocada" = "$APP" ]; then
        swaymsg "[app_id=\"$APP\"] kill" >/dev/null
    else
        swaymsg "[app_id=\"$APP\"] focus" >/dev/null
    fi
    exit 0
fi

exec iwgtk
