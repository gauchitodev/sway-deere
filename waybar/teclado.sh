#!/bin/sh
# Toque en el indicador de teclado de la barra: alterna US <-> LATAM.
# Usa el mismo archivo que los atajos de Carlos (Super+N / Super+M).
f="$HOME/.local/state/sway-kb-layout"
[ "$(cat "$f" 2>/dev/null)" = latam ] && nuevo=us || nuevo=latam
mkdir -p "$(dirname "$f")"
echo "$nuevo" > "$f"
swaymsg input type:keyboard xkb_layout "$nuevo" >/dev/null
pkill -RTMIN+1 waybar
