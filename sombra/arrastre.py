#!/usr/bin/env python3
"""Panel de estado G5 que sigue el dedo, como la cortina de notificaciones de Android.
Lee la pantalla táctil en vivo (sin agarrarla) y mueve la ventana del panel con sway.
Arrastrar desde el borde de arriba lo baja; con el panel abierto, arrastrar hacia arriba lo sube.
Al soltar termina solo (según la velocidad y hasta dónde llegó). Uso: arrastre.py /dev/input/eventN"""
import fcntl, json, os, select, socket, struct, subprocess, sys, time
from collections import deque

APP = "chrome-localhost__sombra-Default"
ALTO = 330            # alto del panel (igual que en sway.conf)
BORDE = 60            # px desde arriba donde tiene que empezar el dedo para bajarlo
UMBRAL = 12           # px que hay que mover antes de que cuente como arrastre
VEL_LANZAR = 500      # px/s: un envión rápido decide abrir/cerrar aunque no llegue a la mitad
ABRIR = os.path.expanduser("~/.config/g5/sombra/abrir.sh")

EV_SYN, EV_ABS = 0, 3
ABS_X, ABS_Y, ABS_SLOT, ABS_MT_X, ABS_MT_Y, ABS_TRACKING = 0x00, 0x01, 0x2F, 0x35, 0x36, 0x39


class Sway:
    def __init__(self):
        self.s = socket.socket(socket.AF_UNIX)
        self.s.connect(os.environ["SWAYSOCK"])

    def _leer(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c:
                raise EOFError
            buf += c
        return buf

    def pedir(self, tipo, carga=""):
        b = carga.encode()
        self.s.sendall(b"i3-ipc" + struct.pack("=II", len(b), tipo) + b)
        n, _ = struct.unpack("=II", self._leer(14)[6:])
        return json.loads(self._leer(n))

    def cmd(self, c):
        return self.pedir(0, c)

    def ventana(self):
        def buscar(n):
            if n.get("app_id") == APP:
                return n
            for k in n.get("nodes", []) + n.get("floating_nodes", []):
                r = buscar(k)
                if r:
                    return r
        return buscar(self.pedir(4))


def facil_salida(t):  # frena suave al final, como el panel de Android
    return 1 - (1 - t) ** 3


class Panel:
    def __init__(self, sway, alto_pantalla):
        self.sway, self.alto_pantalla = sway, alto_pantalla
        self.estado = "libre"   # libre | armado_abrir | armado_cerrar | arrastrando | ignorar
        self.pos = -ALTO
        self.desfase = 0        # y real de la ventana cuando pos = 0 (la barra de arriba)
        self.muestras = deque()

    def mover(self, pos):
        pos = max(-ALTO, min(0, int(pos)))
        if pos != self.pos:
            self.pos = pos
            self.sway.cmd(f'[app_id="{APP}"] move position 0 px {pos} px')

    # --- eventos del dedo ---
    def toque(self, x, y):
        self.x0, self.y0 = x, y
        self.muestras.clear()
        v = self.sway.ventana()
        if v is not None and v.get("visible"):
            self.estado = "armado_cerrar"
        elif y <= BORDE:
            self.estado = "armado_abrir" if v is not None else "sin_ventana"
        else:
            self.estado = "ignorar"
        self.pos = 0 if self.estado == "armado_cerrar" else -ALTO

    def mueve(self, x, y):
        t = time.monotonic()
        self.muestras.append((t, y))
        while self.muestras and t - self.muestras[0][0] > 0.1:
            self.muestras.popleft()
        dy, dx = y - self.y0, x - self.x0
        if self.estado == "armado_abrir" and dy > UMBRAL and dy > abs(dx):
            # Mostrarla escondida arriba y empezar a bajarla
            self.sway.cmd(f'[app_id="{APP}"] scratchpad show, resize set width 100 ppt height {ALTO} px, '
                          f'move position 0 px -{ALTO} px')
            v = self.sway.ventana()
            self.desfase = (v["rect"]["y"] + ALTO) if v else 0
            self.pos = -ALTO
            self.estado = "arrastrando"
            self.ref = None
            self.modo = "abrir"
        elif self.estado == "armado_cerrar" and -dy > UMBRAL and -dy > abs(dx):
            self.estado = "arrastrando"
            self.modo = "cerrar"
            self.ref = y
            v = self.sway.ventana()
            self.desfase = (v["rect"]["y"]) if v else 0
        elif self.estado in ("armado_abrir", "armado_cerrar") and abs(dx) > UMBRAL and abs(dx) > abs(dy):
            self.estado = "ignorar"
        if self.estado == "arrastrando":
            if self.modo == "abrir":   # el borde de abajo del panel sigue al dedo
                self.mover(y - ALTO - self.desfase)
            else:
                self.mover(min(0, y - self.ref))

    def suelta(self):
        est, self.estado = self.estado, "libre"
        if est == "sin_ventana":      # todavía no estaba precargada: que lo haga el script
            subprocess.Popen([ABRIR, "abrir"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            return
        if est != "arrastrando":
            return
        v = 0.0
        if len(self.muestras) >= 2:
            (t0, y0), (t1, y1) = self.muestras[0], self.muestras[-1]
            if t1 > t0:
                v = (y1 - y0) / (t1 - t0)
        if v > VEL_LANZAR:
            abrir = True
        elif v < -VEL_LANZAR:
            abrir = False
        else:
            abrir = self.pos > -ALTO / 2
        self.terminar(abrir)

    def terminar(self, abrir):
        destino = 0 if abrir else -ALTO
        ini = self.pos
        dist = abs(destino - ini)
        if dist:
            dur = max(0.08, 0.26 * dist / ALTO)
            t0 = time.monotonic()
            while True:
                t = (time.monotonic() - t0) / dur
                if t >= 1:
                    break
                self.mover(ini + (destino - ini) * facil_salida(t))
                time.sleep(0.006)
        self.mover(destino)
        if not abrir:
            self.sway.cmd(f'[app_id="{APP}"] move scratchpad')


def rango(fd, eje):
    # EVIOCGABS: struct input_absinfo { value, min, max, fuzz, res, flat }
    buf = bytearray(24)
    fcntl.ioctl(fd, (2 << 30) | (24 << 16) | (0x45 << 8) | (0x40 + eje), buf)
    _, mn, mx, *_ = struct.unpack("6i", buf)
    return mn, mx


def main():
    sway = Sway()
    pantalla = next(o for o in sway.pedir(3) if o.get("active"))["rect"]
    alto_p, ancho_p = pantalla["height"], pantalla["width"]
    fd = os.open(sys.argv[1], os.O_RDONLY)
    ex, ey = (ABS_MT_X, ABS_MT_Y)
    try:
        mxx, mxy = rango(fd, ex), rango(fd, ey)
    except OSError:
        ex, ey = ABS_X, ABS_Y
        mxx, mxy = rango(fd, ex), rango(fd, ey)
    panel = Panel(sway, alto_p)
    slot, crudo, activo = 0, {ex: 0, ey: 0}, False
    tocando, nuevo = False, False
    fmt = struct.Struct("llHHi")
    while True:
        datos = os.read(fd, fmt.size * 64)
        for i in range(0, len(datos) - fmt.size + 1, fmt.size):
            _, _, tipo, cod, val = fmt.unpack_from(datos, i)
            if tipo == EV_ABS:
                if cod == ABS_SLOT:
                    slot = val
                elif slot != 0:
                    continue            # solo sigo el primer dedo
                elif cod == ABS_TRACKING:
                    if val >= 0:
                        nuevo = True
                    else:
                        if tocando:
                            panel.suelta()
                        tocando = False
                elif cod in (ex, ey):
                    crudo[cod] = val
            elif tipo == EV_SYN and cod == 0 and (tocando or nuevo):
                x = (crudo[ex] - mxx[0]) / max(1, mxx[1] - mxx[0]) * ancho_p
                y = (crudo[ey] - mxy[0]) / max(1, mxy[1] - mxy[0]) * alto_p
                if nuevo:
                    nuevo, tocando = False, True
                    panel.toque(x, y)
                else:
                    panel.mueve(x, y)


if __name__ == "__main__":
    main()
