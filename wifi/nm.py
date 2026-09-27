#!/usr/bin/env python3
"""Wifi por NetworkManager (D-Bus), para las compus que no usan iwd solo.

Uso:
  nm.py listar                      redes cerca y guardadas, en JSON
  nm.py estado                      nombre de la red conectada (o nada)
  nm.py buscar                      pide un escaneo
  nm.py conectar "Red"              se conecta a una red guardada
  echo -n "clave" | nm.py nueva "Red"   se conecta a una red nueva y la guarda
  nm.py olvidar "Red"               borra la red guardada
  nm.py desconectar

La contraseña entra por la entrada estándar y va a NetworkManager por D-Bus,
así no queda a la vista en la lista de procesos (ps).
Sale con 0 si anduvo; si no, 1 y el error en stderr.
"""
import json
import sys
import time

from gi.repository import Gio, GLib

NM = "org.freedesktop.NetworkManager"
RUTA = "/org/freedesktop/NetworkManager"
AJUSTES = RUTA + "/Settings"
PROPS = "org.freedesktop.DBus.Properties"

# Flags de seguridad de los puntos de acceso (NM80211ApSecurityFlags)
PRIVACIDAD = 0x1
PSK, EAP, SAE, OWE = 0x100, 0x200, 0x400, 0x800

bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)


def llamar(ruta, interfaz, metodo, args=None, tipo=None, espera=5000):
    r = bus.call_sync(NM, ruta, interfaz, metodo, args,
                      GLib.VariantType(tipo) if tipo else None, 0, espera, None)
    return r.unpack() if tipo else None


def props(ruta, interfaz):
    return llamar(ruta, PROPS, "GetAll", GLib.Variant("(s)", (interfaz,)), "(a{sv})")[0]


def texto(ssid):
    return bytes(ssid).decode("utf-8", "replace")[:40]


def placa():
    """La primera placa wifi que maneja NetworkManager."""
    for d in llamar(RUTA, NM, "GetDevices", None, "(ao)")[0]:
        p = props(d, NM + ".Device")
        if p.get("DeviceType") == 2:   # NM_DEVICE_TYPE_WIFI
            return d
    raise SystemExit("NetworkManager no tiene placa wifi")


def seguridad(ap):
    claves = ap.get("WpaFlags", 0) | ap.get("RsnFlags", 0)
    if claves & EAP:
        return "8021x"
    if claves & (PSK | SAE):
        return "psk"
    if claves & OWE:
        return "owe"
    if ap.get("Flags", 0) & PRIVACIDAD:
        return "wep"
    return "open"


def puntos(dev):
    """Puntos de acceso cerca, uno por nombre (el de mejor señal)."""
    activo = props(dev, NM + ".Device.Wireless").get("ActiveAccessPoint", "/")
    redes = {}
    for ruta in llamar(dev, NM + ".Device.Wireless", "GetAllAccessPoints", None, "(ao)")[0]:
        try:
            ap = props(ruta, NM + ".AccessPoint")
        except GLib.Error:
            continue   # desapareció mientras leíamos
        nombre = texto(ap.get("Ssid", b""))
        if not nombre:
            continue   # redes ocultas
        viejo = redes.get(nombre)
        if not viejo or ap.get("Strength", 0) > viejo["ap"].get("Strength", 0):
            redes[nombre] = {"ruta": ruta, "ap": ap, "conectada": (viejo or {}).get("conectada", False)}
        redes[nombre]["conectada"] = redes[nombre]["conectada"] or ruta == activo
    return redes


def guardadas():
    """{nombre de la red: [rutas de las conexiones guardadas]}"""
    todas = {}
    for c in llamar(AJUSTES, NM + ".Settings", "ListConnections", None, "(ao)")[0]:
        try:
            a = llamar(c, NM + ".Settings.Connection", "GetSettings", None, "(a{sa{sv}})")[0]
        except GLib.Error:
            continue
        if a.get("connection", {}).get("type") != "802-11-wireless":
            continue
        nombre = texto(a.get("802-11-wireless", {}).get("ssid", b""))
        if nombre:
            todas.setdefault(nombre, []).append(c)
    return todas


def esperar(activa, espera=45):
    """Espera a que la conexión quede andando (o falle)."""
    fin = time.time() + espera
    while time.time() < fin:
        try:
            estado = llamar(activa, PROPS, "Get", GLib.Variant("(ss)", (NM + ".Connection.Active", "State")), "(v)")[0]
        except GLib.Error:
            return False   # NetworkManager la sacó: falló
        if estado == 2:    # ACTIVATED
            return True
        if estado == 4:    # DEACTIVATED
            return False
        time.sleep(0.5)
    return False


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    que, red = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    dev = placa()

    if que == "listar":
        cerca = puntos(dev)
        print(json.dumps({
            "redes": sorted(({"nombre": n, "seguridad": seguridad(r["ap"]),
                              "dbm": round(r["ap"].get("Strength", 0) / 2 - 100), "conectada": r["conectada"]}
                             for n, r in cerca.items()), key=lambda r: -r["dbm"]),
            "conocidas": list(guardadas()),
        }))
        return 0
    if que == "estado":
        activo = props(dev, NM + ".Device.Wireless").get("ActiveAccessPoint", "/")
        if activo != "/":
            print(texto(props(activo, NM + ".AccessPoint").get("Ssid", b"")))
        return 0
    if que == "buscar":
        llamar(dev, NM + ".Device.Wireless", "RequestScan", GLib.Variant("(a{sv})", ({},)))
        return 0
    if que == "desconectar":
        llamar(dev, NM + ".Device", "Disconnect")
        return 0
    if not red:
        print("Falta el nombre de la red", file=sys.stderr)
        return 2
    if que == "olvidar":
        for c in guardadas().get(red, []):
            llamar(c, NM + ".Settings.Connection", "Delete")
        return 0
    if que == "conectar":
        conexiones = guardadas().get(red)
        if not conexiones:
            print("Esa red no está guardada", file=sys.stderr)
            return 1
        ap = puntos(dev).get(red, {}).get("ruta", "/")
        activa = llamar(RUTA, NM, "ActivateConnection", GLib.Variant("(ooo)", (conexiones[0], dev, ap)), "(o)")[0]
        return 0 if esperar(activa) else 1
    if que == "nueva":
        r = puntos(dev).get(red)
        if not r:
            print("No encuentro esa red. Buscá redes de nuevo.", file=sys.stderr)
            return 1
        clave = sys.stdin.read() if not sys.stdin.isatty() else ""
        ajustes = {"802-11-wireless": {"ssid": GLib.Variant("ay", red.encode())}}
        tipo = seguridad(r["ap"])
        if tipo == "psk":
            claves = r["ap"].get("WpaFlags", 0) | r["ap"].get("RsnFlags", 0)
            ajustes["802-11-wireless-security"] = {
                "key-mgmt": GLib.Variant("s", "wpa-psk" if claves & PSK else "sae"),
                "psk": GLib.Variant("s", clave)}
        elif tipo != "open":
            print("Ese tipo de red no se puede desde acá", file=sys.stderr)
            return 1
        try:
            conexion, activa = llamar(RUTA, NM, "AddAndActivateConnection",
                                      GLib.Variant("(a{sa{sv}}oo)", (ajustes, dev, r["ruta"])), "(oo)", 20000)
        except GLib.Error as e:
            print(Gio.DBusError.get_remote_error(e) or e.message, file=sys.stderr)
            return 1
        if esperar(activa):
            return 0
        # Clave mal o no conectó: no dejamos guardada una red que no anda
        try:
            llamar(conexion, NM + ".Settings.Connection", "Delete")
        except GLib.Error:
            pass
        print("No se pudo conectar (¿la contraseña?)", file=sys.stderr)
        return 1
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GLib.Error as e:
        print(Gio.DBusError.get_remote_error(e) or e.message, file=sys.stderr)
        sys.exit(1)
