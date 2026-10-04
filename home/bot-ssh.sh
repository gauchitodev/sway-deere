#!/bin/sh
# Entra por SSH al equipo de un bot (local/config.json → "bots"), siempre con la IP al día:
# si el celular cambió de red, la pantalla de inicio lo encuentra y actualiza la config.
#   bot-ssh.sh                → te deja adentro del primer bot
#   bot-ssh.sh menchobot estado  → corre ese comando allá y vuelve
# Cuál bot: G5_BOT=id, o el nombre del enlace (ln -s bot-ssh.sh ~/.local/bin/<id>), o el primero.
CONFIG="$HOME/.config/g5/local/config.json"
set -- "$(python3 -c '
import json, sys
cfg = json.load(open(sys.argv[1]))
bots = cfg.get("bots")
if not isinstance(bots, list):
    b = cfg.get("bot")
    bots = [dict(b, id=b.get("id") or "bot", huella=b.get("huella") or "g5-bot")] if isinstance(b, dict) else []
bots = [b for b in bots if isinstance(b, dict)]
b = next((b for b in bots if b.get("id") in (sys.argv[2], sys.argv[3])), bots[0] if bots else {})
print(b.get("destino", "") or "-", b.get("puerto", 22), b.get("huella") or "g5-bot-" + str(b.get("id", "")))' \
    "$CONFIG" "${G5_BOT:-}" "$(basename "$0")" 2>/dev/null)" "$@"
destino=${1%% *}; resto=${1#* }; puerto=${resto%% *}; huella=${resto#* }; shift
if [ -z "$destino" ] || [ "$destino" = "-" ]; then
    echo "No hay bot configurado en $CONFIG"
    exit 1
fi
# La huella guardada con nombre fijo (la agrega server.py): así no pregunta de nuevo cada vez que cambia la IP
if ssh-keygen -F "$huella" >/dev/null 2>&1; then
    set -- -o HostKeyAlias="$huella" -o StrictHostKeyChecking=yes -- "$destino" "$@"
else
    set -- -- "$destino" "$@"
fi
ssh -p "$puerto" "$@"
codigo=$?
if [ "$codigo" = 255 ]; then
    echo
    echo "No pude entrar a ${destino#*@}. Abrí la pantalla de inicio (Super+G), tocá el panel del bot"
    echo "y después \"Buscar en la red\". Cuando lo encuentre, probá de nuevo."
    # Abierto desde el menú: que no se cierre la ventana antes de leer el aviso
    printf "Enter para cerrar… "; read -r _
fi
exit "$codigo"
