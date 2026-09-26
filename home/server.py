#!/usr/bin/env python3
"""Servidor local de la pantalla de inicio G5.

Lee datos reales de la laptop (/proc, /sys, swaymsg, pamixer) y los entrega
a la página index.html. Solo escucha en 127.0.0.1 y solo lanza los programas
de la lista LANZADORES.
"""
import glob
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from deere import Deere
from lugares import Mapa

# Cada usuario usa su propio puerto (8765 el primero, 8766 el segundo...), así la pantalla
# de inicio de una persona nunca se conecta al servidor de otra que quedó andando.
PUERTO = int(os.environ.get("G5_PUERTO", 8765 + max(0, os.getuid() - 1000) % 1000))
CARPETA = os.path.dirname(os.path.abspath(__file__))
ESTADO = os.environ.get("G5_ESTADO") or os.path.join(CARPETA, "estado.json")
# Modo demo (para sacar capturas sin datos personales): G5_DEMO=1
DEMO = os.environ.get("G5_DEMO") == "1"
TOKEN = secrets.token_hex(16)

# Lo que pueden lanzar los botones de la barra de abajo.
# "volver": primero esconde la pantalla de inicio, así la app abre en tu escritorio y no queda tapada.
LANZADORES = {
    "terminal":  {"cmd": "foot", "volver": True},
    "navegador": {"cmd": "chromium", "volver": True},
    "apps":      {"cmd": "wofi --show drun", "volver": True},
    "volumen":   {"cmd": "pavucontrol", "volver": True},
    "captura":   {"cmd": "mkdir -p ~/Pictures/Screenshots && f=~/Pictures/Screenshots/ss-$(date +%F_%H-%M-%S).png && grim \"$f\" && wl-copy -t image/png < \"$f\"", "volver": True},
    "bloquear":  {"cmd": "swaylock -f -c 000000", "volver": False},
    "cerrar":    {"cmd": None, "volver": True},
    # Acciones de la categoría "Sistema" del menú
    "wifi":          {"cmd": os.path.expanduser("~/.config/g5/wifi/abrir.sh"), "volver": True},
    "brillo_mas":    {"cmd": "brightnessctl s 10%+", "volver": False},
    "brillo_menos":  {"cmd": "brightnessctl s 10%-", "volver": False},
    "recargar":      {"cmd": "swaymsg reload", "volver": False},
    "monitor":       {"cmd": "foot -e htop", "volver": True},
    "suspender":     {"cmd": "swaylock -f -c 000000 && systemctl suspend", "volver": False},
    "cerrar_sesion": {"cmd": "swaymsg exit", "volver": False},
    "reiniciar":     {"cmd": "systemctl reboot", "volver": False},
    "apagar":        {"cmd": "systemctl poweroff", "volver": False},
}

# Configuración de esta compu (no se sube al repo): ver config.ejemplo.json
LOCAL = os.path.join(os.path.dirname(CARPETA), "local", "config.json")
try:
    with open(LOCAL) as _f:
        CONFIG = json.load(_f)
except (OSError, ValueError):
    CONFIG = {}
# Panel "Bot": un celular o servidor al que se entra por SSH con llave.
# El comando remoto sale con 0 si el bot anda (y puede imprimir los segundos que lleva),
# con 1 si está detenido. Cualquier otra cosa = sin conexión.
BOT = CONFIG.get("bot") or {}
if DEMO:
    BOT = {"nombre": "Mi bot", "dispositivo": "el celular"}

# Nombres más claros para algunas apps
NOMBRES = {
    "foot.desktop": "Terminal",
    "org.pulseaudio.pavucontrol.desktop": "Volumen",
    "htop.desktop": "Monitor del sistema",
    "gmail.desktop": "Correo",
}
# Favoritas de arranque (después se eligen con la estrella)
FAVORITAS = [
    "chromium.desktop", "foot.desktop", "gmail.desktop", "docs.desktop",
    "sheets.desktop", "maps.desktop", "youtube.desktop", "ytmusic.desktop",
    "org.pulseaudio.pavucontrol.desktop", "htop.desktop",
]


def leer(ruta, defecto=None):
    try:
        with open(ruta) as f:
            return f.read().strip()
    except OSError:
        return defecto


def leer_int(ruta, defecto=None):
    v = leer(ruta)
    try:
        return int(v)
    except (TypeError, ValueError):
        return defecto


def correr(cmd, timeout=2):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------- Sensores ----------

_cpu_prev = None


def cpu_uso():
    global _cpu_prev
    partes = leer("/proc/stat", "").splitlines()[0].split()[1:]
    nums = [int(x) for x in partes]
    inactivo = nums[3] + nums[4]
    total = sum(nums)
    uso = 0
    if _cpu_prev:
        dt = total - _cpu_prev[0]
        di = inactivo - _cpu_prev[1]
        uso = round(100 * (dt - di) / dt) if dt else 0
    _cpu_prev = (total, inactivo)
    return uso


_top_cache = {"t": 0, "cpu": None, "mem": None}


def procesos_top():
    if time.time() - _top_cache["t"] > 5:
        def primero(orden):
            filas = correr(["ps", "-eo", "comm,pcpu,rss", "--sort=" + orden, "--no-headers"]).splitlines()
            for f in filas:
                partes = f.split()
                if len(partes) >= 3 and partes[0] not in ("ps", "python3"):
                    return {"nombre": partes[0], "cpu": float(partes[1]), "mb": round(int(partes[2]) / 1024)}
            return None
        _top_cache.update(t=time.time(), cpu=primero("-pcpu"), mem=primero("-rss"))
    return _top_cache["cpu"], _top_cache["mem"]


def cpu_modelo():
    for linea in leer("/proc/cpuinfo", "").splitlines():
        if linea.startswith("model name"):
            m = re.search(r"(Ryzen \d+|Core i\d+|Core Ultra \d+|Xeon|Athlon|Celeron|Pentium)", linea)
            return m.group(1) if m else linea.split(":", 1)[1].strip()[:24]
    return "Procesador"


def sistema():
    for linea in leer("/etc/os-release", "").splitlines():
        if linea.startswith("NAME="):
            return linea.split("=", 1)[1].strip('"') + " · Sway"
    return "Linux · Sway"


def cpu_detalle():
    frec = [leer_int(f) for f in glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")]
    frec = [f for f in frec if f]
    maxf = leer_int("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")
    carga = leer("/proc/loadavg", "0 0 0").split()[:3]
    return {
        "ghz": round(sum(frec) / len(frec) / 1e6, 2) if frec else None,
        "ghz_max": round(maxf / 1e6, 1) if maxf else None,
        "carga": [float(x) for x in carga],
    }


def memoria():
    info = {}
    for linea in leer("/proc/meminfo", "").splitlines():
        k, v = linea.split(":", 1)
        info[k] = int(v.split()[0])
    total = info.get("MemTotal", 0) / 1048576
    usada = (info.get("MemTotal", 0) - info.get("MemAvailable", 0)) / 1048576
    swap = (info.get("SwapTotal", 0) - info.get("SwapFree", 0)) / 1048576
    return round(usada, 1), round(total, 1), round(swap, 1)


def bateria():
    base = next(iter(glob.glob("/sys/class/power_supply/BAT*")), None)
    if not base:
        return None
    pct = leer_int(base + "/capacity", 0)
    estado = leer(base + "/status", "Unknown")
    ahora = leer_int(base + "/charge_now") or leer_int(base + "/energy_now")
    lleno = leer_int(base + "/charge_full") or leer_int(base + "/energy_full")
    corriente = leer_int(base + "/current_now") or leer_int(base + "/power_now")
    minutos = None
    if corriente and ahora is not None:
        if estado == "Discharging":
            minutos = round(60 * ahora / corriente)
        elif estado == "Charging" and lleno:
            minutos = round(60 * (lleno - ahora) / corriente)
    lleno_dis = leer_int(base + "/charge_full_design") or leer_int(base + "/energy_full_design")
    volt = leer_int(base + "/voltage_now")
    watts = None
    if corriente is not None:
        # current_now viene en µA (con voltage_now en µV) o power_now directo en µW
        watts = round(corriente * volt / 1e12, 1) if volt and leer_int(base + "/current_now") else round(corriente / 1e6, 1)
    temp = leer_int(base + "/temp")
    return {
        "pct": pct, "estado": estado, "minutos": minutos,
        "salud": round(100 * lleno / lleno_dis) if lleno and lleno_dis else None,
        "temp": round(temp / 10) if temp else None,
        "watts": watts,
        "ciclos": leer_int(base + "/cycle_count") or None,
    }


def temp_de(nombre):
    for n in glob.glob("/sys/class/hwmon/*/name"):
        if leer(n) == nombre:
            t = leer_int(os.path.join(os.path.dirname(n), "temp1_input"))
            return round(t / 1000) if t is not None else None
    return None


def temperatura():
    for n in glob.glob("/sys/class/hwmon/*/name"):
        if leer(n) == "k10temp":
            t = leer_int(os.path.join(os.path.dirname(n), "temp1_input"))
            if t is not None:
                return round(t / 1000)
    return None


_red_cache = {"t": 0, "ssid": None}
RED_ARCHIVO = os.environ.get("G5_RED") or os.path.join(CARPETA, "red.json")
_red_uso = {"bajada": 0, "subida": 0, "dias": {}}


def _contar_red():
    """Suma los bytes del wifi por día (para ver cuánto hotspot gastaste) y mide la velocidad."""
    try:
        with open(RED_ARCHIVO) as f:
            _red_uso["dias"] = json.load(f)
    except (OSError, ValueError):
        pass
    antes = None
    ultimo_guardado = time.time()
    # Si la laptop arrancó hoy y todavía no hay cuenta de hoy, sumamos lo gastado desde el arranque
    hoy = date.today().isoformat()
    arranque = time.time() - float(leer("/proc/uptime", "0").split()[0])
    if hoy not in _red_uso["dias"] and date.fromtimestamp(arranque).isoformat() == hoy:
        _red_uso["dias"][hoy] = (leer_int("/sys/class/net/wlan0/statistics/rx_bytes", 0)
                                 + leer_int("/sys/class/net/wlan0/statistics/tx_bytes", 0))
    while True:
        rx = leer_int("/sys/class/net/wlan0/statistics/rx_bytes", 0)
        tx = leer_int("/sys/class/net/wlan0/statistics/tx_bytes", 0)
        ahora = time.time()
        if antes:
            drx = rx - antes[0] if rx >= antes[0] else rx   # si se reinició el contador
            dtx = tx - antes[1] if tx >= antes[1] else tx
            dt = ahora - antes[2]
            _red_uso["bajada"] = round(drx * 8 / dt / 1e6, 2)   # Mbps
            _red_uso["subida"] = round(dtx * 8 / dt / 1e6, 2)
            hoy = date.today().isoformat()
            _red_uso["dias"][hoy] = _red_uso["dias"].get(hoy, 0) + drx + dtx
        antes = (rx, tx, ahora)
        if ahora - ultimo_guardado > 60:
            viejos = (date.today() - timedelta(days=60)).isoformat()
            _red_uso["dias"] = {d: b for d, b in _red_uso["dias"].items() if d >= viejos}
            with open(RED_ARCHIVO, "w") as f:
                json.dump(_red_uso["dias"], f)
            ultimo_guardado = ahora
        time.sleep(3)


def uso_red():
    hoy = date.today()
    semana = [(hoy - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    # En el demo la carga del internet queda en el rojo, así las capturas lucen los colores
    bajada, subida = (0.82, 0.12) if DEMO else (_red_uso["bajada"], _red_uso["subida"])
    return {
        "bajada": bajada, "subida": subida,
        "hoy_mb": round(_red_uso["dias"].get(hoy.isoformat(), 0) / 1048576),
        "semana": [{"dia": d, "mb": round(_red_uso["dias"].get(d, 0) / 1048576)} for d in semana],
    }


def red():
    dbm = None
    for linea in leer("/proc/net/wireless", "").splitlines()[2:]:
        partes = linea.split()
        if len(partes) > 3:
            try:
                dbm = int(float(partes[3]))
            except ValueError:
                pass
            break
    if time.time() - _red_cache["t"] > 30:
        salida = re.sub(r"\x1b\[[0-9;]*m", "", correr(["iwctl", "station", "wlan0", "show"]))
        m = re.search(r"Connected network\s+(.+)", salida)
        _red_cache.update(t=time.time(), ssid=m.group(1).strip() if m else None)
    rx = leer_int("/sys/class/net/wlan0/statistics/rx_bytes", 0)
    tx = leer_int("/sys/class/net/wlan0/statistics/tx_bytes", 0)
    return {"dbm": dbm, "ssid": "MiWifi" if DEMO else _red_cache["ssid"], "mb": round((rx + tx) / 1048576)}


def disco():
    s = os.statvfs(os.path.expanduser("~"))
    total = s.f_blocks * s.f_frsize / 1e9
    libre = s.f_bavail * s.f_frsize / 1e9
    return {"pct": round(100 * (total - libre) / total), "libre": round(libre), "total": round(total)}


def brillo():
    base = next(iter(glob.glob("/sys/class/backlight/*")), None)
    if not base:
        return None
    b, m = leer_int(base + "/brightness"), leer_int(base + "/max_brightness")
    return round(100 * b / m) if b is not None and m else None


def volumen():
    v = correr(["pamixer", "--get-volume"]).strip()
    mudo = correr(["pamixer", "--get-mute"]).strip() == "true"
    return {"pct": int(v) if v.isdigit() else None, "mudo": mudo}


def musica():
    formato = ("{{status}}\t{{title}}\t{{artist}}\t{{mpris:artUrl}}\t{{playerName}}"
               "\t{{xesam:album}}\t{{position}}\t{{mpris:length}}")
    salida = correr(["playerctl", "metadata", "--format", formato], timeout=2).strip()
    if not salida:
        return None
    partes = (salida.split("\t") + [""] * 8)[:8]
    estado, titulo, artista, portada, reproductor, album, pos, largo = partes
    if portada.startswith("file://"):
        portada = "/portada?v=" + urllib.parse.quote(portada[-12:])
    # playerctl da microsegundos; al navegador le mandamos segundos
    seg = lambda v: round(int(v) / 1e6, 1) if v.isdigit() else None
    return {"estado": estado, "titulo": titulo, "artista": artista, "portada": portada,
            "reproductor": reproductor, "album": album, "pos": seg(pos), "largo": seg(largo)}


def portada_local():
    url = correr(["playerctl", "metadata", "mpris:artUrl"], timeout=2).strip()
    if not url.startswith("file://"):
        return None
    ruta = os.path.realpath(urllib.parse.unquote(url[7:]))
    # Solo las portadas temporales que guarda Chromium, nada más del disco
    if not os.path.basename(ruta).startswith(".org.chromium.Chromium.") or not ruta.startswith("/tmp/"):
        return None
    try:
        with open(ruta, "rb") as f:
            return f.read()
    except OSError:
        return None


# ---------- YouTube: últimos videos de los canales elegidos ----------

_yt_cache = {"t": 0, "canales": None, "videos": []}


def yt_resolver(handle):
    """De @canal saca el id del canal leyendo su página pública."""
    handle = handle.strip().lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,40}", handle):
        return None
    try:
        req = urllib.request.Request("https://www.youtube.com/@" + handle,
                                     headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "es"})
        html = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace")
    except OSError:
        return None
    m = re.search(r'"externalId":"(UC[A-Za-z0-9_-]{22})"', html) or re.search(r'"channelId":"(UC[A-Za-z0-9_-]{22})"', html)
    nombre = re.search(r'<meta property="og:title" content="([^"]+)"', html)
    if not m:
        return None
    return {"handle": "@" + handle, "id": m.group(1), "nombre": nombre.group(1) if nombre else "@" + handle}


def yt_videos(canales):
    clave = json.dumps(canales, sort_keys=True)
    if _yt_cache["canales"] == clave and time.time() - _yt_cache["t"] < 1800:
        return _yt_cache["videos"]
    ns = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
    videos = []
    for c in canales:
        try:
            xml = urllib.request.urlopen(
                "https://www.youtube.com/feeds/videos.xml?channel_id=" + c["id"], timeout=10).read()
            for e in ET.fromstring(xml).findall("a:entry", ns)[:6]:
                videos.append({
                    "id": e.findtext("yt:videoId", "", ns),
                    "titulo": e.findtext("a:title", "", ns),
                    "canal": c["nombre"],
                    "fecha": e.findtext("a:published", "", ns),
                })
        except (OSError, ET.ParseError):
            continue
    videos.sort(key=lambda v: v["fecha"], reverse=True)
    _yt_cache.update(t=time.time(), canales=clave, videos=videos[:12])
    return _yt_cache["videos"]


# ---------- Bluetooth (con bluetoothctl) ----------

MAC = re.compile(r"([0-9A-F]{2}(?::[0-9A-F]{2}){5})")
_bt_cache = {"t": 0, "datos": None}
_bt_trabajo = {"que": None, "mac": None, "error": None}   # lo que se está haciendo ahora (tarda unos segundos)
_bt_candado = threading.Lock()


def _bt_lista(*filtro):
    """Devuelve {mac: nombre} de `bluetoothctl devices [Paired|Connected]`."""
    lista = {}
    filtro = [f for f in filtro if f]
    for linea in correr(["bluetoothctl", "devices", *filtro], timeout=3).splitlines():
        partes = linea.split(" ", 2)
        if len(partes) == 3 and partes[0] == "Device" and MAC.fullmatch(partes[1]):
            lista[partes[1]] = partes[2].strip()
    return lista


def bluetooth():
    if DEMO:
        return {"hay": True, "prendido": True, "trabajo": dict(_bt_trabajo), "encontrados": [],
                "dispositivos": [{"mac": "00:00:00:00:00:01", "nombre": "Auriculares", "conectado": True, "bateria": 80},
                                 {"mac": "00:00:00:00:00:02", "nombre": "Parlante", "conectado": False, "bateria": None}]}
    if time.time() - _bt_cache["t"] > 4 or _bt_cache["datos"] is None:
        show = correr(["bluetoothctl", "show"], timeout=3)
        if "Controller" not in show:
            datos = {"hay": False}
        else:
            vinculados = _bt_lista("Paired")
            conectados = _bt_lista("Connected")
            dispositivos = []
            for mac, nombre in vinculados.items():
                bateria = None
                if mac in conectados:
                    m = re.search(r"Battery Percentage:.*\((\d+)\)", correr(["bluetoothctl", "info", mac], timeout=3))
                    bateria = int(m.group(1)) if m else None
                dispositivos.append({"mac": mac, "nombre": nombre, "conectado": mac in conectados, "bateria": bateria})
            dispositivos.sort(key=lambda x: (not x["conectado"], x["nombre"].lower()))
            # Encontrados al buscar: los que no están vinculados y tienen nombre de verdad
            encontrados = [{"mac": m, "nombre": n} for m, n in _bt_lista().items()
                           if m not in vinculados and n.replace("-", ":") != m]
            datos = {"hay": True, "prendido": "Powered: yes" in show,
                     "dispositivos": dispositivos, "encontrados": encontrados[:8]}
        _bt_cache.update(t=time.time(), datos=datos)
    return dict(_bt_cache["datos"], trabajo=dict(_bt_trabajo))


def _bt_hacer(que, mac):
    """Corre la acción en un hilo aparte: conectar o vincular puede tardar 10 segundos."""
    pasos = {
        "prender": [["power", "on"]],
        "apagar": [["power", "off"]],
        "buscar": [["--timeout", "12", "scan", "on"]],
        "conectar": [["connect", mac]],
        "desconectar": [["disconnect", mac]],
        "vincular": [["pair", mac], ["trust", mac], ["connect", mac]],
    }[que]
    for p in pasos:
        try:
            r = subprocess.run(["bluetoothctl", *p], capture_output=True, text=True, timeout=30)
            salida = r.stdout + r.stderr
        except (OSError, subprocess.SubprocessError):
            salida, r = "", None
        # Ojo: no buscar "Error" suelto, el nombre de un aparato cercano podría contenerlo
        if r is None or r.returncode != 0 or re.search(r"^Failed|org\.bluez\.Error|not available", salida, re.M):
            _bt_trabajo["error"] = que
            break
    _bt_trabajo.update(que=None, mac=None)
    _bt_cache["t"] = 0


def bt_accion(que, mac):
    """Valida el pedido de la página: solo MACs que bluetoothctl ya conoce."""
    if DEMO or que not in ("prender", "apagar", "buscar", "conectar", "desconectar", "vincular"):
        return False
    if que in ("prender", "apagar", "buscar"):
        mac = None
    elif not isinstance(mac, str) or not MAC.fullmatch(mac):
        return False
    elif mac not in _bt_lista("Paired" if que != "vincular" else None):
        return False
    with _bt_candado:
        if _bt_trabajo["que"]:
            return False
        _bt_trabajo.update(que=que, mac=mac, error=None)
    threading.Thread(target=_bt_hacer, args=(que, mac), daemon=True).start()
    return True


_sway_cache = {"t": 0, "ventanas": 0, "escritorios": 0}


def ventanas():
    if time.time() - _sway_cache["t"] > 5:
        try:
            arbol = json.loads(correr(["swaymsg", "-t", "get_tree"]) or "{}")
        except ValueError:
            arbol = {}
        escritorios = set()

        def recorrer(nodo, ws=None):
            if nodo.get("type") == "workspace":
                ws = nodo.get("name")
            if nodo.get("pid") and not (nodo.get("app_id") or "").startswith("chrome-127.0.0.1"):
                escritorios.add(ws)
                cuenta[0] += 1
            for h in nodo.get("nodes", []) + nodo.get("floating_nodes", []):
                recorrer(h, ws)

        cuenta = [0]
        recorrer(arbol)
        _sway_cache.update(t=time.time(), ventanas=cuenta[0], escritorios=len(escritorios - {"__i3_scratch"}))
    return {"ventanas": _sway_cache["ventanas"], "escritorios": _sway_cache["escritorios"]}


_amfbot = {"estado": "revisando" if BOT else "sin_configurar", "segundos": None, "revisado": None,
           "nombre": BOT.get("nombre", "Bot"), "dispositivo": BOT.get("dispositivo", "el celular"),
           "trabajo": None, "error": None}
_amfbot_ya = threading.Event()   # "revisá ahora", sin esperar los 30 s
_amfbot_buscar = threading.Event()   # "buscalo en la red ahora"
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?")

# Si el celular cambia de red, cambia de IP. Para encontrarlo sin riesgo de entrar a otro aparato,
# su huella SSH se guarda en known_hosts con este nombre fijo: ssh la compara antes de mandar la llave.
ALIAS = "g5-bot"


def _alias_listo():
    """True si known_hosts ya tiene la huella del bot con el nombre fijo. Si no, la copia de la
    entrada de la IP actual (la que quedó guardada la primera vez que entraste)."""
    if subprocess.run(["ssh-keygen", "-F", ALIAS], capture_output=True).returncode == 0:
        return True
    host, puerto = BOT["destino"].split("@")[-1], BOT.get("puerto", 22)
    r = subprocess.run(["ssh-keygen", "-F", host if puerto == 22 else f"[{host}]:{puerto}"], capture_output=True, text=True)
    lineas = [ln.split(" ", 1)[1] for ln in r.stdout.splitlines() if ln and not ln.startswith("#") and " " in ln]
    if not lineas:
        return False
    with open(os.path.expanduser("~/.ssh/known_hosts"), "a") as f:
        f.writelines(f"{ALIAS} {ln}\n" for ln in lineas)
    return True


def _ssh_base(destino):
    ssh = ["ssh", "-p", str(BOT.get("puerto", 22)), "-o", "BatchMode=yes", "-o", "ConnectTimeout=6"]
    if _amfbot.get("alias"):
        ssh += ["-o", f"HostKeyAlias={ALIAS}", "-o", "StrictHostKeyChecking=yes"]
    return ssh + [destino]


def _bot_ssh(comando, timeout=20):
    return subprocess.run(_ssh_base(BOT["destino"]) + [comando], capture_output=True, text=True, timeout=timeout)


def _redes_locales():
    """Las IPs de las redes /24 (o más chicas) donde está conectada esta compu, sin la propia."""
    ips = []
    for linea in correr(["ip", "-4", "-o", "addr", "show", "scope", "global"]).splitlines():
        partes = linea.split()
        if len(partes) < 4:
            continue
        try:
            yo = ipaddress.ip_interface(partes[3])
        except ValueError:
            continue
        red = yo.network if yo.network.prefixlen >= 24 else ipaddress.ip_network(f"{yo.ip}/24", strict=False)
        ips += [str(h) for h in red.hosts() if h != yo.ip]
    return ips


def _puerto_abierto(ip):
    try:
        with socket.create_connection((ip, BOT.get("puerto", 22)), timeout=0.6):
            return ip
    except OSError:
        return None


def _es_tailscale(destino):
    """Las direcciones de Tailscale (100.64.0.0/10) no cambian: si el bot está en una, no se busca en el wifi."""
    try:
        return ipaddress.ip_address(destino.split("@")[-1]) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def _buscar_bot():
    """Busca el celular en la red: primero quién tiene el puerto abierto, después prueba SSH con la
    huella guardada. Solo devuelve una IP si la huella coincide (si no, ssh corta antes de entrar)."""
    usuario = BOT["destino"].split("@")[0] + "@" if "@" in BOT["destino"] else ""
    with ThreadPoolExecutor(64) as pool:
        abiertos = [ip for ip in pool.map(_puerto_abierto, _redes_locales()) if ip]
    for ip in abiertos:
        try:
            r = subprocess.run(_ssh_base(usuario + ip) + ["true"], capture_output=True, timeout=15)
            if r.returncode == 0:
                return ip
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def _bot_info():
    """Lo que ve la página: estado más a qué equipo pregunta y qué se puede hacer."""
    info = dict(_amfbot)
    if BOT.get("destino"):
        info["host"] = BOT["destino"].split("@")[-1]
        info["puerto"] = BOT.get("puerto", 22)
    info["puede_arrancar"] = bool(BOT.get("arrancar"))
    info["puede_buscar"] = bool(info.get("alias"))
    return info


def _vigilar_amfbot():
    """Cada 30 s pregunta al celular si el bot está andando (en un hilo aparte)."""
    if DEMO:
        _amfbot.update(estado="andando", segundos=3 * 86400 + 5 * 3600, revisado=time.strftime("%H:%M"))
        return
    if not BOT:
        return
    ultima_busqueda = 0
    while True:
        _amfbot_ya.clear()
        if not _amfbot.get("alias"):
            try:
                _amfbot["alias"] = _alias_listo()
            except OSError:
                pass
        try:
            r = _bot_ssh(BOT["comando"])
            lineas = [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]
            # Una línea que es solo un número = segundos andando; las demás son detalles para mostrar.
            # Si una empieza con ⚠ el bot anda pero algo no está bien (luz amarilla).
            detalle = [ln[:120] for ln in lineas if not ln.isdigit()][:4]
            _amfbot.update(detalle=detalle, alerta=any(ln.startswith("⚠") for ln in detalle))
            if r.returncode == 0:
                nums = [int(ln) for ln in lineas if ln.isdigit()]
                estado, seg = "andando", (nums[0] if nums else None)
            elif r.returncode == 1:
                estado, seg = "detenido", None
            else:
                estado, seg = "sin_conexion", None
        except (OSError, subprocess.SubprocessError):
            estado, seg = "sin_conexion", None
        _amfbot.update(estado=estado, segundos=seg, revisado=time.strftime("%H:%M"))
        if estado != "sin_conexion" and _amfbot.get("error") == "buscar":
            _amfbot["error"] = None
        if _amfbot.get("trabajo") == "revisar":
            _amfbot["trabajo"] = None
        # Sin conexión: lo busca en la red solo (como mucho cada 2 min) o cuando se lo pedís
        pedido = _amfbot_buscar.is_set()
        if _amfbot.get("alias") and (pedido or (estado == "sin_conexion" and not _es_tailscale(BOT["destino"])
                                                and time.time() - ultima_busqueda > 120)):
            _amfbot_buscar.clear()
            ultima_busqueda = time.time()
            _amfbot.update(trabajo="buscar", error=None)
            ip = _buscar_bot()
            _amfbot["trabajo"] = None
            if ip and ip != BOT["destino"].split("@")[-1] and _bot_cambiar_host(ip):
                _amfbot["encontrado"] = ip
                continue   # revisa de nuevo ya, con la IP nueva
            if not ip:
                _amfbot["error"] = "buscar"
        _amfbot_ya.wait(30)


def _bot_arrancar():
    try:
        r = _bot_ssh(BOT["arrancar"], timeout=30)
        _amfbot["error"] = None if r.returncode == 0 else "arrancar"
    except (OSError, subprocess.SubprocessError):
        _amfbot["error"] = "arrancar"
    time.sleep(8)   # que el proceso termine de levantar antes de preguntar
    _amfbot["trabajo"] = "revisar"
    _amfbot_ya.set()


def _bot_cambiar_host(host):
    """Cambia la IP (o nombre) del celular en local/config.json, sin tocar el resto."""
    try:
        with open(LOCAL) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return False
    if not isinstance(cfg.get("bot"), dict):
        return False
    usuario = BOT["destino"].split("@")[0] + "@" if "@" in BOT["destino"] else ""
    BOT["destino"] = cfg["bot"]["destino"] = usuario + host
    tmp = LOCAL + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.chmod(tmp, os.stat(LOCAL).st_mode & 0o777)
        os.replace(tmp, LOCAL)
    except OSError:
        return False
    return True


def bot_accion(que, valor=None):
    if DEMO or not BOT.get("destino"):
        return False
    if que != "terminal" and _amfbot.get("trabajo"):
        return False
    if que == "revisar":
        _amfbot.update(trabajo="revisar", error=None)
        _amfbot_ya.set()
        return True
    if que == "buscar" and _amfbot.get("alias"):
        _amfbot.update(trabajo="buscar", error=None)
        _amfbot_buscar.set()
        _amfbot_ya.set()
        return True
    if que == "arrancar" and BOT.get("arrancar"):
        _amfbot.update(trabajo="arrancar", error=None)
        threading.Thread(target=_bot_arrancar, daemon=True).start()
        return True
    if que == "terminal":
        cmd = shlex.join(["foot", "-T", BOT.get("nombre", "Bot"), os.path.join(CARPETA, "bot-ssh.sh")])
        esconder_inicio()
        subprocess.Popen(["swaymsg", "exec", "--", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    if que == "ip" and isinstance(valor, str) and HOST.fullmatch(valor.strip()):
        if not _bot_cambiar_host(valor.strip()):
            return False
        _amfbot.update(estado="revisando", segundos=None, trabajo="revisar", error=None)
        _amfbot_ya.set()
        return True
    return False


def datos():
    usada, total, swap = memoria()
    top_cpu, top_mem = procesos_top()
    return {
        "cpu": cpu_uso(),
        "cpu_det": cpu_detalle(),
        "cpu_modelo": cpu_modelo(),
        "sistema": sistema(),
        "top_cpu": top_cpu,
        "top_mem": top_mem,
        "swap": swap,
        "temp_gpu": temp_de("amdgpu"),
        "temp_disco": temp_de("nvme"),
        "uso_red": uso_red(),
        "musica": musica(),
        "hilos": os.cpu_count(),
        "ram": usada,
        "ram_total": total,
        "bateria": bateria(),
        "temp": temperatura(),
        "red": red(),
        "disco": disco(),
        "brillo": brillo(),
        "volumen": volumen(),
        "sway": ventanas(),
        "amfbot": _bot_info(),
        "temporizador": {"fin": _timer["fin"], "min": _timer["min"]} if _timer["fin"] else None,
        "bluetooth": bluetooth(),
        "encendida_min": round(float(leer("/proc/uptime", "0").split()[0]) / 60),
        "equipo": socket.gethostname(),
        "kernel": os.uname().release,
    }


# ---------- Apps instaladas (archivos .desktop) ----------

def _leer_desktop(ruta):
    campos, en_entrada = {}, False
    try:
        with open(ruta, encoding="utf-8", errors="replace") as f:
            for linea in f:
                linea = linea.strip()
                if linea.startswith("["):
                    en_entrada = linea == "[Desktop Entry]"
                    continue
                if en_entrada and "=" in linea:
                    k, v = linea.split("=", 1)
                    campos.setdefault(k.strip(), v.strip())
    except OSError:
        return None
    if campos.get("Type") != "Application" or "Exec" not in campos:
        return None
    if campos.get("NoDisplay", "").lower() == "true" or campos.get("Hidden", "").lower() == "true":
        return None
    return campos


def apps():
    carpetas = ["/usr/share/applications", os.path.expanduser("~/.local/share/applications")]
    encontradas = {}
    for carpeta in carpetas:  # las del usuario pisan a las del sistema
        for ruta in sorted(glob.glob(os.path.join(carpeta, "*.desktop"))):
            c = _leer_desktop(ruta)
            if c:
                ident = os.path.basename(ruta)
                encontradas[ident] = {
                    "id": ident,
                    "nombre": NOMBRES.get(ident, c.get("Name", ident)),
                    "categorias": [x for x in c.get("Categories", "").split(";") if x],
                    "terminal": c.get("Terminal", "").lower() == "true",
                    "exec": c["Exec"],
                    "icono": icono_archivo(c.get("Icon", "")),
                }
    return encontradas


def lanzar_app(ident):
    app = apps().get(ident)
    if not app:
        return False
    esconder_inicio()
    if app["terminal"]:
        # Las apps de consola (htop, vim...) se abren dentro de foot
        comando = re.sub(r"\s%[fFuUdDnNickvm]", "", app["exec"])
        cmd = "foot -e " + comando
    else:
        cmd = "gtk-launch " + ident[:-len(".desktop")]
    subprocess.Popen(["swaymsg", "exec", "--", cmd], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# Íconos propios de las apps (los de las apps web: Gmail, Deere...). Solo imágenes que
# estén en las carpetas de íconos; el resto de las apps usa el dibujo del menú.
CARPETAS_ICONOS = [os.path.realpath(os.path.expanduser(c)) for c in
                   ("~/.local/share/icons", "/usr/share/icons", "/usr/share/pixmaps")]
TIPOS_ICONO = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


def icono_archivo(icon):
    """Ruta del ícono si el .desktop apunta a una imagen válida, si no None."""
    if not icon.startswith("/"):
        return None
    ruta = os.path.realpath(icon)
    if os.path.splitext(ruta)[1].lower() not in TIPOS_ICONO:
        return None
    if not any(ruta.startswith(c + os.sep) for c in CARPETAS_ICONOS):
        return None
    try:
        if not 0 < os.path.getsize(ruta) <= 2_000_000:
            return None
    except OSError:
        return None
    return ruta


# ---------- Paneles web (local/config.json → "web") ----------
# Cada panel tiene botones que abren direcciones fijas de la config, con la misma
# app web (.desktop) que dice "app": mismo perfil del navegador y mismas opciones.

def _web_leer():
    paneles = {}
    for w in CONFIG.get("web") or []:
        if not isinstance(w, dict):
            continue
        ident = w.get("id")
        if not isinstance(ident, str) or not re.fullmatch(r"[a-z]{1,17}", ident) or ident in paneles:
            continue
        enlaces = [{"nombre": e["nombre"][:30], "url": e["url"]} for e in (w.get("enlaces") or [])
                   if isinstance(e, dict) and isinstance(e.get("nombre"), str) and isinstance(e.get("url"), str)
                   and re.fullmatch(r"https://[^\s\"'<>]+", e["url"])][:8]
        arch = w.get("archivos") if isinstance(w.get("archivos"), dict) else None
        if arch:
            try:
                patron = re.compile(str(arch.get("patron", ".")), re.I)
            except re.error:
                patron = None
            carpetas = [os.path.expanduser(c) for c in arch.get("carpetas", []) if isinstance(c, str)][:4]
            arch = {"titulo": str(arch.get("titulo") or "Archivos")[:40], "patron": patron,
                    "carpetas": carpetas, "dias": max(1, min(365, arch["dias"])) if type(arch.get("dias")) is int else 30} \
                if patron and carpetas else None
        paneles[ident] = {"id": ident, "titulo": str(w.get("titulo") or ident)[:30],
                          "app": w.get("app") if isinstance(w.get("app"), str) else None,
                          "enlaces": enlaces, "archivos": arch}
    return paneles


WEB = _web_leer()


def web_lista():
    todas = apps()
    return [{"id": w["id"], "titulo": w["titulo"],
             "icono": w["app"] if w["app"] in todas and todas[w["app"]]["icono"] else None,
             "enlaces": [e["nombre"] for e in w["enlaces"]],
             "archivos": w["archivos"]["titulo"] if w["archivos"] else None} for w in WEB.values()]


def web_archivos(ident):
    """Los últimos archivos que coinciden con el patrón (solo nombre, fecha y tamaño)."""
    w = WEB.get(ident)
    if not w or not w["archivos"]:
        return None
    a, hallados = w["archivos"], []
    limite = time.time() - a["dias"] * 86400
    for carpeta in a["carpetas"]:
        try:
            with os.scandir(carpeta) as it:
                for f in it:
                    if f.is_file(follow_symlinks=False) and a["patron"].search(f.name):
                        st = f.stat(follow_symlinks=False)
                        if st.st_mtime >= limite:
                            hallados.append((st.st_mtime, f.name, st.st_size, f.path))
        except OSError:
            continue
    hallados.sort(reverse=True)
    return hallados[:6]


def web_abrir(ident, n):
    w = WEB.get(ident)
    if not w or not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < len(w["enlaces"]):
        return False
    return abrir_url(w["enlaces"][n]["url"], w["app"])


def abrir_url(url, app_id=None):
    """Abre la dirección como app web, con el mismo perfil y opciones que el .desktop que se diga."""
    app = apps().get(app_id) if app_id else None
    argv = ["chromium", "--app=" + url]
    if app and not app["terminal"]:
        try:
            argv = [x for x in shlex.split(app["exec"]) if not re.fullmatch(r"%[fFuUdDnNickvm]", x)]
        except ValueError:
            pass
        else:
            i = next((i for i, x in enumerate(argv) if x.startswith("--app=")), None)
            if i is None:
                argv.append(url)
            else:
                argv[i] = "--app=" + url
    esconder_inicio()
    subprocess.Popen(["swaymsg", "exec", "--", shlex.join(argv)],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def web_copiar(ident, nombre):
    """Copia la ruta completa de uno de los archivos listados (para pegarla al subirlo)."""
    for _, n, _, ruta in web_archivos(ident) or []:
        if n == nombre:
            subprocess.run(["wl-copy", "--", ruta], capture_output=True, timeout=3)
            return True
    return False


# ---------- Mini Operations Center y mini Files (API de Deere, solo lectura) ----------

DEERE_CFG = CONFIG.get("deere") if isinstance(CONFIG.get("deere"), dict) else {}
DEERE = Deere(os.path.join(os.path.dirname(CARPETA), "local"), DEERE_CFG, DEMO)
OPC = "https://operationscenter.deere.com/"
FILES = "https://files.deere.com/"


def _app_cfg(clave):
    v = DEERE_CFG.get(clave)
    return v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,80}\.desktop", v) else None


def deere_accion(que, ident=None):
    if que == "actualizar":
        DEERE.ver_flota(forzar=True)
        DEERE.ver_archivos(forzar=True)
        return True
    if que == "org" and isinstance(ident, str):
        return DEERE.cambiar_org(ident)
    if que == "opc":
        return abrir_url(OPC, _app_cfg("app_opc"))
    if que == "files":
        return abrir_url(FILES + "#/prescriptions" if ident == "rx" else FILES, _app_cfg("app_files"))
    if que == "llegar" and isinstance(ident, str):
        m = DEERE.maquina(ident)
        if not m or not m["lugar"]:
            return False
        # Las coordenadas salen de la lista del servidor, no de la página
        destino = f'{m["lugar"]["lat"]:.6f},{m["lugar"]["lon"]:.6f}'
        return abrir_url("https://www.google.com/maps/dir/?api=1&destination=" + destino, "maps.desktop")
    return False


MAPA = Mapa(lambda: estado_leer(), lambda c: estado_mezclar(c), DEMO)   # se definen más abajo


def mapa_google():
    """Abre en Google Maps lo que muestra el panel (el lugar lo sabe el servidor, no la página)."""
    m = MAPA.ver()
    q = f'{m["lat"]},{m["lon"]}' if m.get("lat") is not None else m.get("nombre", "")
    return abrir_url("https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote(q, safe=","), "maps.desktop")


# ---------- Lo que muestran los menús de cada panel ----------

def procesos(orden):
    """Los programas que más usan, sumando los procesos del mismo nombre (Chromium tiene muchos)."""
    grupos = {}
    for f in correr(["ps", "-eo", "comm,pcpu,rss", "--no-headers"]).splitlines():
        partes = f.rsplit(None, 2)
        if len(partes) != 3:
            continue
        nombre, cpu, rss = partes
        if nombre.strip() == "ps":   # el propio ps que acabamos de correr
            continue
        try:
            g = grupos.setdefault(nombre.strip()[:30], {"nombre": nombre.strip()[:30], "cpu": 0.0, "mb": 0, "n": 0})
            g["cpu"] += float(cpu)
            g["mb"] += int(rss) // 1024
            g["n"] += 1
        except ValueError:
            continue
    lista = sorted(grupos.values(), key=lambda g: -(g["cpu"] if orden == "cpu" else g["mb"]))[:7]
    for g in lista:
        g["cpu"] = round(g["cpu"] / (os.cpu_count() or 1), 1)   # % de toda la compu, no de un núcleo
    return lista


def sensores():
    lista = []
    nombres = {"k10temp": "Procesador", "amdgpu": "Placa de video", "nvme": "Disco (SSD)",
               "acpitz": "Placa madre", "iwlwifi_1": "Wifi", "rtw88_8821ce": "Wifi"}
    for n in sorted(glob.glob("/sys/class/hwmon/*/name")):
        base, chip = os.path.dirname(n), leer(n, "")
        for t in sorted(glob.glob(base + "/temp*_input")):
            v = leer_int(t)
            if v is None:
                continue
            etiqueta = leer(t.replace("_input", "_label"), "")
            lista.append({"nombre": nombres.get(chip, chip)[:24] + (f" · {etiqueta[:16]}" if etiqueta else ""),
                          "c": round(v / 1000)})
    return lista[:12]


# Wifi (iwd): redes cerca y conectarse a las que ya conocés
_wifi = {"t": 0, "redes": [], "conocidas": [], "trabajo": None, "error": None}
_wifi_candado = threading.Lock()
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _wifi_leer():
    salida = ANSI.sub("", correr(["iwctl", "station", "wlan0", "get-networks", "rssi-dbms"], timeout=4))
    redes = []
    for linea in salida.splitlines():
        m = re.match(r"^\s*(>)?\s+(.+?)\s{2,}(psk|open|8021x|wep|owe)\s+(-?\d+)\s*$", linea)
        if m:
            redes.append({"nombre": m.group(2)[:40], "seguridad": m.group(3),
                          "dbm": round(int(m.group(4)) / 100), "conectada": bool(m.group(1))})
    conocidas = []
    for linea in ANSI.sub("", correr(["iwctl", "known-networks", "list"], timeout=4)).splitlines():
        m = re.match(r"^\s{2}(.+?)\s{2,}(psk|open|8021x|wep|owe)\s", linea)
        if m:
            conocidas.append(m.group(1)[:40])
    _wifi.update(t=time.time(), redes=redes[:12], conocidas=conocidas)


def wifi_ip():
    m = re.search(r"inet (\S+)", correr(["ip", "-4", "-o", "addr", "show", "wlan0"], timeout=3))
    return m.group(1) if m else None


def wifi_info():
    if DEMO:
        return {"redes": [{"nombre": "MiWifi", "seguridad": "psk", "dbm": -48, "conectada": True, "conocida": True},
                          {"nombre": "Vecino", "seguridad": "psk", "dbm": -80, "conectada": False, "conocida": False}],
                "trabajo": None, "error": None}
    if time.time() - _wifi["t"] > 8:
        _wifi_leer()
    return {"redes": [dict(r, conocida=r["nombre"] in _wifi["conocidas"]) for r in _wifi["redes"]],
            "trabajo": _wifi["trabajo"], "error": _wifi["error"]}


WIFI_CONECTAR = os.path.join(os.path.dirname(CARPETA), "wifi", "conectar.py")


def _wifi_hacer(que, red, clave=""):
    cmd = {"buscar": ["iwctl", "station", "wlan0", "scan"],
           "conectar": ["iwctl", "station", "wlan0", "connect", red or ""],
           # Red nueva: la contraseña va por stdin, así no se ve en la lista de procesos
           "nueva": ["python3", WIFI_CONECTAR, red or ""],
           "olvidar": ["iwctl", "known-networks", red or "", "forget"],
           "desconectar": ["iwctl", "station", "wlan0", "disconnect"]}[que]
    try:
        r = subprocess.run(cmd, input=clave, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            _wifi["error"] = que
    except (OSError, subprocess.SubprocessError):
        _wifi["error"] = que
    if que == "buscar":
        time.sleep(4)   # el escaneo sigue un rato después de que iwctl contesta
    _wifi.update(t=0, trabajo=None)
    _red_cache["t"] = 0


def wifi_accion(que, red=None, clave=None):
    if DEMO or que not in ("buscar", "conectar", "nueva", "olvidar", "desconectar", "otra"):
        return False
    if que == "otra":
        # Abre la ventana de wifi (el panel de wifi no tiene menú propio)
        esconder_inicio()
        subprocess.Popen(["swaymsg", "exec", "--", os.path.expanduser("~/.config/g5/wifi/abrir.sh")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    if que == "conectar" and (not isinstance(red, str) or red not in _wifi["conocidas"]
                              or red not in [r["nombre"] for r in _wifi["redes"]]):
        return False
    if que == "olvidar" and (not isinstance(red, str) or red not in _wifi["conocidas"]):
        return False
    if que == "nueva":
        cerca = {r["nombre"]: r["seguridad"] for r in _wifi["redes"]}
        if not isinstance(red, str) or red not in cerca or cerca[red] not in ("psk", "open"):
            return False
        clave = clave if isinstance(clave, str) else ""
        # WPA: de 8 a 63 letras (o 64 hexadecimales)
        if cerca[red] == "psk" and not (8 <= len(clave) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", clave)):
            return False
        if cerca[red] == "open":
            clave = ""
    with _wifi_candado:
        if _wifi["trabajo"]:
            return False
        _wifi.update(trabajo=que, error=None)
    threading.Thread(target=_wifi_hacer, args=(que, red, clave if que == "nueva" else ""), daemon=True).start()
    return True


# Disco: qué carpetas ocupan más (du tarda, se calcula aparte y se guarda 10 min)
_du = {"t": 0, "carpetas": [], "calculando": False}
PAPELERA = os.path.expanduser("~/.local/share/Trash")


def _du_calcular():
    casa = os.path.expanduser("~")
    lista = []
    for linea in correr(["du", "-x", "-d1", "-B1M", casa], timeout=90).splitlines():
        partes = linea.split("\t", 1)
        if len(partes) == 2 and partes[0].isdigit() and partes[1] != casa:
            lista.append({"nombre": "~/" + os.path.basename(partes[1])[:40], "mb": int(partes[0])})
    lista.sort(key=lambda c: -c["mb"])
    _du.update(t=time.time(), carpetas=lista[:8], calculando=False)


def _tam_mb(carpeta):
    total = 0
    for raiz, _, archivos in os.walk(carpeta):
        for a in archivos:
            try:
                total += os.lstat(os.path.join(raiz, a)).st_size
            except OSError:
                pass
    return round(total / 1048576)


def disco_info():
    if not _du["calculando"] and time.time() - _du["t"] > 600:
        _du["calculando"] = True
        threading.Thread(target=_du_calcular, daemon=True).start()
    return {"carpetas": _du["carpetas"], "calculando": _du["calculando"],
            "papelera_mb": _tam_mb(os.path.join(PAPELERA, "files"))}


def vaciar_papelera():
    for sub in ("files", "info"):
        carpeta = os.path.join(PAPELERA, sub)
        try:
            with os.scandir(carpeta) as it:
                for f in it:
                    if f.is_dir(follow_symlinks=False):
                        shutil.rmtree(f.path, ignore_errors=True)
                    else:
                        try:
                            os.unlink(f.path)
                        except OSError:
                            pass
        except OSError:
            continue
    _du["t"] = 0
    return True


# Ventanas abiertas: ir a una o cerrarla
def ventanas_lista():
    if DEMO:
        return [{"id": 1, "app": "chromium", "titulo": "Operations Center", "escritorio": "1"},
                {"id": 2, "app": "foot", "titulo": "Terminal", "escritorio": "2"}]
    try:
        arbol = json.loads(correr(["swaymsg", "-t", "get_tree"]) or "{}")
    except ValueError:
        return []
    lista = []

    def recorrer(nodo, ws=None):
        if nodo.get("type") == "workspace":
            ws = nodo.get("name")
        app = nodo.get("app_id") or (nodo.get("window_properties") or {}).get("class") or ""
        if nodo.get("pid") and not app.startswith("chrome-127.0.0.1"):
            lista.append({"id": nodo["id"], "app": app[:40], "titulo": (nodo.get("name") or "")[:90],
                          "escritorio": "" if ws == "__i3_scratch" else str(ws or "")[:20]})
        for h in nodo.get("nodes", []) + nodo.get("floating_nodes", []):
            recorrer(h, ws)

    recorrer(arbol)
    return lista[:30]


def ventana_accion(que, ident):
    if DEMO or que not in ("ir", "cerrar") or not isinstance(ident, int) or isinstance(ident, bool):
        return False
    if ident not in [v["id"] for v in ventanas_lista()]:
        return False
    if que == "ir":
        esconder_inicio()
        correr(["swaymsg", f"[con_id={ident}] focus"])
    else:
        correr(["swaymsg", f"[con_id={ident}] kill"])
    _sway_cache["t"] = 0
    return True


# Salidas de sonido (parlantes, auriculares Bluetooth...)
def salidas():
    try:
        lista = json.loads(correr(["pactl", "-f", "json", "list", "sinks"], timeout=3) or "[]")
    except ValueError:
        lista = []
    defecto = correr(["pactl", "get-default-sink"]).strip()
    return [{"nombre": str(s.get("name", ""))[:120], "desc": str(s.get("description") or s.get("name", ""))[:60],
             "activa": s.get("name") == defecto} for s in lista if isinstance(s, dict)][:8]


def salida_elegir(nombre):
    if DEMO or not isinstance(nombre, str) or nombre not in [s["nombre"] for s in salidas()]:
        return False
    correr(["pactl", "set-default-sink", nombre])
    return True


# Temporizador del reloj: lo lleva el servidor, así suena aunque la pantalla de inicio esté cerrada
_timer = {"fin": None, "min": None, "evento": None}


def _timer_esperar(evento, fin, minutos):
    if evento.wait(max(0, fin - time.time())):
        return   # lo cancelaron
    _timer.update(fin=None, min=None, evento=None)
    txt = f"Pasaron {minutos} minuto{'s' if minutos != 1 else ''}"
    subprocess.run(["notify-send", "-u", "critical", "-a", "G5", "Temporizador", txt], capture_output=True, timeout=5)


def temporizador(minutos):
    if _timer["evento"]:
        _timer["evento"].set()
        _timer.update(fin=None, min=None, evento=None)
    if minutos is None:
        return True
    if not isinstance(minutos, int) or isinstance(minutos, bool) or not 1 <= minutos <= 600:
        return False
    ev = threading.Event()
    fin = time.time() + minutos * 60
    _timer.update(fin=fin, min=minutos, evento=ev)
    threading.Thread(target=_timer_esperar, args=(ev, fin, minutos), daemon=True).start()
    return True


def red_mes():
    hoy = date.today()
    dias = [(hoy - timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    return [{"dia": d, "mb": round(_red_uso["dias"].get(d, 0) / 1048576)} for d in dias]


def extra(k):
    """Datos que solo hacen falta con el menú de un panel abierto (algunos cuestan más de calcular)."""
    if k == "cpu":
        return {"procesos": procesos("cpu")}
    if k == "ram":
        return {"procesos": procesos("mem")}
    if k == "temp":
        return {"sensores": sensores()}
    if k == "net":
        return {"mes": red_mes()}
    if k == "disk":
        return disco_info()
    if k == "win":
        return {"ventanas": ventanas_lista()}
    if k in ("vol", "musica"):
        return {"salidas": salidas()}
    return {}


def hoja_accion(k, cuerpo):
    que = cuerpo.get("que")
    if k == "wifi":
        return wifi_accion(que, cuerpo.get("red"))
    if k == "win":
        return ventana_accion(que, cuerpo.get("id"))
    if k == "disk" and que == "papelera":
        return not DEMO and vaciar_papelera()
    if k in ("vol", "musica") and que == "salida":
        return salida_elegir(cuerpo.get("nombre"))
    if k == "bright" and que == "poner":
        v = cuerpo.get("pct")
        if DEMO or not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= 100:
            return False
        correr(["brightnessctl", "-q", "s", f"{v}%"])
        return True
    if k == "reloj" and que == "timer":
        return temporizador(cuerpo.get("min"))
    return False


# ---------- Huevo de pascua ----------

def exif_foto(ruta):
    """Lee del EXIF de la foto la fecha y las coordenadas GPS (si las tiene)."""
    try:
        d = open(ruta, "rb").read()
    except OSError:
        return {}
    i = 2
    while i + 4 < len(d) and d[i] == 0xFF:
        marca, largo = d[i + 1], struct.unpack(">H", d[i + 2:i + 4])[0]
        if marca == 0xE1 and d[i + 4:i + 10] == b"Exif\0\0":
            t = d[i + 10:i + 2 + largo]
            e = "<" if t[:2] == b"II" else ">"

            def ifd(off):
                n = struct.unpack(e + "H", t[off:off + 2])[0]
                return {tag: (typ, cnt, val) for tag, typ, cnt, val in
                        (struct.unpack(e + "HHI4s", t[off + 2 + 12 * k:off + 14 + 12 * k]) for k in range(n))}

            def texto(tabla, tag):
                typ, cnt, val = tabla[tag]
                off = struct.unpack(e + "I", val)[0] if cnt > 4 else None
                crudo = t[off:off + cnt] if off is not None else val[:cnt]
                return crudo.rstrip(b"\0").decode("ascii", "replace")

            try:
                ifd0 = ifd(struct.unpack(e + "I", t[4:8])[0])
                r = {}
                if 0x0132 in ifd0:
                    r["fecha"] = texto(ifd0, 0x0132)
                if 0x8825 in ifd0:
                    g = ifd(struct.unpack(e + "I", ifd0[0x8825][2])[0])

                    def grados(tag):
                        o = struct.unpack(e + "I", g[tag][2])[0]
                        v = [struct.unpack(e + "II", t[o + 8 * j:o + 8 * j + 8]) for j in range(3)]
                        return v[0][0] / v[0][1] + v[1][0] / v[1][1] / 60 + v[2][0] / v[2][1] / 3600

                    lat, lon = grados(2), grados(4)
                    if g[1][2][:1] == b"S":
                        lat = -lat
                    if g[3][2][:1] == b"W":
                        lon = -lon
                    r.update(lat=round(lat, 6), lon=round(lon, 6))
                return r
            except (struct.error, KeyError, ZeroDivisionError):
                return {}
        i += 2 + largo
    return {}


def huevo():
    raiz = os.path.dirname(CARPETA)
    return [dict(archivo=n, titulo=t, **exif_foto(os.path.join(raiz, n)))
            for n, t in (("bloqueo.jpg", "La S770 de atrás"), ("fondo.jpg", "La S770 al atardecer"))]


# ---------- Estado guardado (paneles y modo día/noche) ----------

_estado_candado = threading.RLock()


def estado_leer():
    try:
        with open(ESTADO) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def estado_mezclar(cambios):
    """Para lo que guarda el propio servidor (el mapa): mezcla sin pasar por el filtro de la página."""
    with _estado_candado:
        e = estado_leer()
        e.update(cambios)
        tmp = ESTADO + ".tmp"
        with open(tmp, "w") as f:
            json.dump(e, f, indent=2)
        os.replace(tmp, ESTADO)


def paginas_limpias(paginas):
    """Deja solo páginas con paneles bien formados: clave corta, tamaño s/m/g y
    lugar (x, y, w, h) como fracciones entre 0 y 1. Devuelve None si no sirve."""
    if not isinstance(paginas, list) or not 1 <= len(paginas) <= 20:
        return None
    limpias = []
    for pag in paginas:
        paneles = pag.get("paneles") if isinstance(pag, dict) else None
        if not isinstance(paneles, list):
            return None
        lista = []
        for p in paneles[:40]:
            if not isinstance(p, dict):
                continue
            k = p.get("k")
            if not isinstance(k, str) or not re.fullmatch(r"[a-z]{1,20}", k):
                continue
            q = {"k": k, "t": p.get("t") if p.get("t") in ("s", "m", "g") else "s"}
            lugar = [p.get(c) for c in ("x", "y", "w", "h")]
            if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and 0 <= v <= 1 for v in lugar):
                q.update(zip(("x", "y", "w", "h"), (round(float(v), 4) for v in lugar)))
            lista.append(q)
        limpias.append({"paneles": lista})
    return limpias


def estado_guardar(nuevo):
    permitido = {k: nuevo[k] for k in ("paginas", "modo", "favoritas", "sonido", "notas", "mapa") if k in nuevo}
    if not isinstance(permitido.get("mapa", ""), str):
        permitido.pop("mapa")
    if not isinstance(permitido.get("notas", ""), str):
        permitido.pop("notas")
    if "paginas" in permitido:
        paginas = paginas_limpias(permitido["paginas"])
        if paginas is None:
            permitido.pop("paginas")
        else:
            permitido["paginas"] = paginas
    with _estado_candado:
        actual = estado_leer()
        if "paginas" in permitido:
            actual.pop("paneles", None)  # formato viejo, de una sola página
        actual.update(permitido)
        with open(ESTADO, "w") as f:
            json.dump(actual, f, indent=2)


# ---------- Lanzar programas ----------

def esconder_inicio():
    """La pantalla de inicio es una capa (el scratchpad de sway): esconderla deja ver tu escritorio,
    y lo que se abra aparece ahí en vez de quedar tapado por la capa."""
    subprocess.run(["swaymsg", '[app_id="^chrome-127\\.0\\.0\\.1"] move scratchpad'], capture_output=True)


def lanzar(nombre):
    item = LANZADORES[nombre]
    if item["volver"]:
        esconder_inicio()
    if item["cmd"]:
        subprocess.Popen(["swaymsg", "exec", "--", item["cmd"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Manejador(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _host_ok(self):
        return self.headers.get("Host") in (f"127.0.0.1:{PUERTO}", f"localhost:{PUERTO}")

    def _token_ok(self):
        return self.headers.get("X-G5-Token") == TOKEN

    def _responder(self, codigo, cuerpo, tipo="application/json"):
        datos_b = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(datos_b)))
        self.end_headers()
        self.wfile.write(datos_b)

    def do_GET(self):
        if not self._host_ok():
            return self._responder(403, {"error": "host"})
        if self.path in ("/", "/index.html"):
            with open(os.path.join(CARPETA, "index.html"), "rb") as f:
                html = f.read().replace(b"__G5_TOKEN__", TOKEN.encode())
            return self._responder(200, html, "text/html; charset=utf-8")
        if self.path == "/wifi":
            # Ventana de wifi (Super+Shift+W o tocar el wifi en la barra)
            with open(os.path.join(os.path.dirname(CARPETA), "wifi", "index.html"), "rb") as f:
                html = f.read().replace(b"__G5_TOKEN__", TOKEN.encode())
            return self._responder(200, html, "text/html; charset=utf-8")
        if self.path.split("?")[0] in ("/fondo.jpg", "/bloqueo.jpg"):
            try:
                with open(os.path.join(os.path.dirname(CARPETA), self.path.split("?")[0][1:]), "rb") as f:
                    return self._responder(200, f.read(), "image/jpeg")
            except OSError:
                return self._responder(404, {"error": "sin fondo"})
        if self.path.startswith("/icono?"):
            # Las imágenes no mandan encabezados: la clave va en la dirección
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if q.get("t", [""])[0] != TOKEN:
                return self._responder(403, {"error": "no autorizado"})
            app = apps().get(q.get("app", [""])[0])
            ruta = app and app["icono"]
            if not ruta:
                return self._responder(404, {"error": "sin ícono"})
            try:
                with open(ruta, "rb") as f:
                    return self._responder(200, f.read(), TIPOS_ICONO[os.path.splitext(ruta)[1].lower()])
            except OSError:
                return self._responder(404, {"error": "sin ícono"})
        if self.path.startswith("/api/") and not self._token_ok():
            # Clave vieja (el servidor se reinició): la página se recarga sola.
            return self._responder(403, {"error": "no autorizado"})
        if self.path == "/api/wifi" and self._token_ok():
            return self._responder(200, dict(wifi_info(), actual=red(), ip=wifi_ip(), modo=estado_leer().get("modo", "dia")))
        if self.path == "/api/datos" and self._token_ok():
            return self._responder(200, datos())
        if self.path == "/api/estado" and self._token_ok():
            e = estado_leer()
            e.setdefault("favoritas", FAVORITAS)
            return self._responder(200, e)
        if self.path.startswith("/portada"):
            img = portada_local()
            return self._responder(200, img, "image/jpeg") if img else self._responder(404, {"error": "sin portada"})
        if self.path == "/api/huevo" and self._token_ok():
            return self._responder(200, huevo())
        if self.path == "/api/youtube" and self._token_ok():
            e = estado_leer()
            if "yt_canales" not in e:
                # Primera vez: arranca con el canal oficial de John Deere
                jd = yt_resolver("@JohnDeere")
                if jd:
                    e["yt_canales"] = [jd]
                    with open(ESTADO, "w") as f:
                        json.dump(e, f, indent=2)
            canales = e.get("yt_canales", [])
            return self._responder(200, {"canales": canales, "videos": yt_videos(canales)})
        if self.path == "/api/apps" and self._token_ok():
            lista = [{"id": a["id"], "nombre": a["nombre"], "categorias": a["categorias"], "icono": bool(a["icono"])}
                     for a in apps().values()]
            return self._responder(200, sorted(lista, key=lambda a: a["nombre"].lower()))
        if self.path.startswith("/api/extra?") and self._token_ok():
            k = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("k", [""])[0]
            return self._responder(200, extra(k))
        if self.path == "/api/mapa" and self._token_ok():
            return self._responder(200, MAPA.ver())
        if self.path == "/api/deere" and self._token_ok():
            if not DEERE.configurado():
                return self._responder(200, {"configurado": False})
            return self._responder(200, {"configurado": True, "orgs": DEERE.lista_orgs(),
                                         "flota": DEERE.ver_flota(), "archivos": DEERE.ver_archivos()})
        if self.path == "/api/web" and self._token_ok():
            return self._responder(200, web_lista())
        if self.path.startswith("/api/web/archivos?") and self._token_ok():
            ident = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("id", [""])[0]
            lista = web_archivos(ident)
            if lista is None:
                return self._responder(404, {"error": "no existe"})
            return self._responder(200, [{"nombre": n, "fecha": round(t), "tam": tam} for t, n, tam, _ in lista])
        self._responder(404, {"error": "no existe"})

    def do_POST(self):
        if not (self._host_ok() and self._token_ok()):
            return self._responder(403, {"error": "no autorizado"})
        try:
            largo = int(self.headers.get("Content-Length", 0))
        except ValueError:
            largo = -1
        if not 0 <= largo <= 200000:
            return self._responder(413, {"error": "tamaño"})
        try:
            cuerpo = json.loads(self.rfile.read(largo) or "{}")
        except ValueError:
            return self._responder(400, {"error": "json"})
        if not isinstance(cuerpo, dict):
            return self._responder(400, {"error": "json"})
        if self.path == "/api/lanzar" and str(cuerpo.get("que", "")).startswith("app:"):
            ok = lanzar_app(cuerpo["que"][4:])
            return self._responder(200 if ok else 404, {"ok": ok})
        if self.path == "/api/musica" and cuerpo.get("accion") in ("play-pause", "next", "previous"):
            correr(["playerctl", cuerpo["accion"]])
            return self._responder(200, {"ok": True})
        if self.path == "/api/musica" and cuerpo.get("accion") == "ir":
            s = cuerpo.get("seg")
            if not isinstance(s, (int, float)) or isinstance(s, bool) or not 0 <= s <= 36000:
                return self._responder(400, {"error": "seg"})
            correr(["playerctl", "position", str(round(float(s), 1))])
            return self._responder(200, {"ok": True})
        if self.path == "/api/musica" and cuerpo.get("accion") == "volumen":
            v = cuerpo.get("pct")
            if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 100:
                return self._responder(400, {"error": "pct"})
            correr(["pamixer", "--unmute", "--set-volume", str(v)])
            return self._responder(200, {"ok": True})
        if self.path == "/api/musica" and cuerpo.get("accion") == "silencio":
            correr(["pamixer", "--toggle-mute"])
            return self._responder(200, {"ok": True})
        if self.path == "/api/video" and re.fullmatch(r"[A-Za-z0-9_-]{11}", str(cuerpo.get("id", ""))):
            esconder_inicio()
            url = "https://www.youtube.com/watch?v=" + cuerpo["id"]
            subprocess.Popen(["swaymsg", "exec", "--", "chromium --app=" + url],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self._responder(200, {"ok": True})
        if self.path == "/api/yt/agregar":
            canal = yt_resolver(str(cuerpo.get("handle", "")))
            if not canal:
                return self._responder(404, {"error": "No encontré ese canal"})
            e = estado_leer()
            lista = [c for c in e.get("yt_canales", []) if c["id"] != canal["id"]] + [canal]
            e["yt_canales"] = lista
            with open(ESTADO, "w") as f:
                json.dump(e, f, indent=2)
            _yt_cache["t"] = 0
            return self._responder(200, canal)
        if self.path == "/api/yt/quitar":
            e = estado_leer()
            e["yt_canales"] = [c for c in e.get("yt_canales", []) if c["id"] != cuerpo.get("id")]
            with open(ESTADO, "w") as f:
                json.dump(e, f, indent=2)
            _yt_cache["t"] = 0
            return self._responder(200, {"ok": True})
        if self.path == "/api/bot":
            ok = bot_accion(cuerpo.get("que"), cuerpo.get("valor"))
            return self._responder(200 if ok else 409, {"ok": ok})
        if self.path == "/api/bluetooth":
            ok = bt_accion(cuerpo.get("que"), cuerpo.get("mac"))
            return self._responder(200 if ok else 409, {"ok": ok})
        if self.path == "/api/web/abrir":
            ok = web_abrir(cuerpo.get("id"), cuerpo.get("n"))
            return self._responder(200 if ok else 404, {"ok": ok})
        if self.path == "/api/web/copiar":
            ok = web_copiar(cuerpo.get("id"), cuerpo.get("nombre"))
            return self._responder(200 if ok else 404, {"ok": ok})
        if self.path == "/api/wifi":
            ok = wifi_accion(cuerpo.get("que"), cuerpo.get("red"), cuerpo.get("clave"))
            return self._responder(200 if ok else 409, {"ok": ok})
        if self.path == "/api/hoja":
            ok = hoja_accion(cuerpo.get("k"), cuerpo)
            return self._responder(200 if ok else 409, {"ok": ok})
        if self.path == "/api/mapa":
            ok = mapa_google() if cuerpo.get("que") == "gmaps" else MAPA.accion(cuerpo.get("que"), cuerpo.get("lugar"))
            return self._responder(200 if ok else 400, {"ok": ok})
        if self.path == "/api/deere":
            ok = DEERE.configurado() and deere_accion(cuerpo.get("que"), cuerpo.get("id"))
            return self._responder(200 if ok else 409, {"ok": ok})
        if self.path == "/api/lanzar" and cuerpo.get("que") in LANZADORES:
            lanzar(cuerpo["que"])
            return self._responder(200, {"ok": True})
        if self.path == "/api/estado" and isinstance(cuerpo, dict):
            estado_guardar(cuerpo)
            return self._responder(200, {"ok": True})
        self._responder(400, {"error": "pedido inválido"})


if __name__ == "__main__":
    threading.Thread(target=_vigilar_amfbot, daemon=True).start()
    threading.Thread(target=_contar_red, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", PUERTO), Manejador).serve_forever()
