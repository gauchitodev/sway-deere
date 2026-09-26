#!/usr/bin/env python3
"""Se conecta a una red wifi nueva por iwd (D-Bus), como hace iwctl.

Uso:  echo -n "contraseña" | conectar.py "Nombre de la red"

La contraseña entra por la entrada estándar y no como argumento, así no queda
a la vista de otros usuarios en la lista de procesos (ps). Para redes abiertas
no hace falta mandar nada.
Sale con 0 si se conectó; si no, 1 y el error de iwd en stderr.
"""
import sys
import warnings

from gi.repository import Gio, GLib

IWD = "net.connman.iwd"
AGENTE = "/g5/wifi/agente"
XML = """<node><interface name="net.connman.iwd.Agent">
  <method name="Release"/>
  <method name="RequestPassphrase"><arg type="o" direction="in"/><arg type="s" direction="out"/></method>
  <method name="Cancel"><arg type="s" direction="in"/></method>
</interface></node>"""


def main():
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    nombre = sys.argv[1]
    clave = sys.stdin.read() if not sys.stdin.isatty() else ""

    bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
    objetos = bus.call_sync(IWD, "/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
                            None, GLib.VariantType("(a{oa{sa{sv}}})"), 0, 5000, None).unpack()[0]
    placa = next((ruta for ruta, ifs in objetos.items()
                  if ifs.get(IWD + ".Device", {}).get("Name") == "wlan0"), None)
    red = next((ruta for ruta, ifs in objetos.items()
                if ifs.get(IWD + ".Network", {}).get("Name") == nombre
                and ifs[IWD + ".Network"].get("Device") == placa), None)
    if not red:
        print("No encuentro esa red. Buscá redes de nuevo.", file=sys.stderr)
        return 1

    # El "agente" es a quien iwd le pregunta la contraseña cuando la necesita
    def pregunta(_con, _de, _ruta, _interfaz, metodo, _args, invocacion):
        if metodo == "RequestPassphrase" and clave:
            invocacion.return_value(GLib.Variant("(s)", (clave,)))
        elif metodo == "RequestPassphrase":
            invocacion.return_dbus_error(IWD + ".Agent.Error.Canceled", "sin contraseña")
        else:
            invocacion.return_value(None)

    warnings.simplefilter("ignore", DeprecationWarning)   # PyGObject nuevo avisa, pero anda igual
    registro = bus.register_object(AGENTE, Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0], pregunta, None, None)
    bus.call_sync(IWD, "/net/connman/iwd", IWD + ".AgentManager", "RegisterAgent",
                  GLib.Variant("(o)", (AGENTE,)), None, 0, 5000, None)

    bucle = GLib.MainLoop()
    resultado = {"error": "tardó demasiado"}

    def listo(con, res):
        try:
            con.call_finish(res)
            resultado["error"] = None
        except GLib.Error as e:
            resultado["error"] = Gio.DBusError.get_remote_error(e) or e.message
        bucle.quit()

    bus.call(IWD, red, IWD + ".Network", "Connect", None, None, 0, 45000, None, listo)
    GLib.timeout_add_seconds(50, bucle.quit)
    bucle.run()

    try:
        bus.call_sync(IWD, "/net/connman/iwd", IWD + ".AgentManager", "UnregisterAgent",
                      GLib.Variant("(o)", (AGENTE,)), None, 0, 5000, None)
    except GLib.Error:
        pass
    bus.unregister_object(registro)

    if resultado["error"]:
        print(resultado["error"], file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
