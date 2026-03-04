import os
import sys
import threading
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime
import json
import urllib.request

APP_TITLE = "Storage Client - Control (CNS) - Robust"
SERVER_IP_FIXED = "127.0.0.1"
SERVER_PORT = 5000

WEB_HOST = "127.0.0.1"
WEB_PORT = 8000

DEPARTAMENTOS = [
    ("La Paz", "LPZ"),
    ("Cochabamba", "CBB"),
    ("Santa Cruz", "SCZ"),
    ("Oruro", "ORU"),
    ("Pando", "PND"),
    ("Chuquisaca", "CHQ"),
    ("Tarija", "TJA"),
    ("Beni", "BEN"),
    ("Potosí", "PTS"),
]

def now_hms():
    return datetime.now().strftime("%H:%M:%S")

def py_executable():
    return sys.executable

def script_dir():
    return os.path.dirname(os.path.abspath(__file__))

def client_script_path():
    return os.path.join(script_dir(), "client.py")

def fetch_nodes(timeout_sec: float = 2.0):
    url = f"http://{WEB_HOST}:{WEB_PORT}/api/nodes"
    try:
        with urllib.request.urlopen(url, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
            return json.loads(raw)
    except Exception:
        return None

def active_node_ids():
    nodes = fetch_nodes()
    if nodes is None:
        return None
    active = set()
    for n in nodes:
        if str(n.get("status", "")).upper() == "ACTIVE":
            active.add(str(n.get("node_id", "")))
    return active

def client_supports_flag(flag: str) -> bool:
    try:
        p = subprocess.run(
            [py_executable(), client_script_path(), "--help"],
            cwd=script_dir(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=3.0
        )
        out = (p.stdout or "") + (p.stderr or "")
        return flag in out
    except Exception:
        return False

def decode_line(b: bytes) -> str:
    """
    Decodificación robusta para Windows/PowerShell/acentos:
    intenta utf-8 -> cp1252 -> latin-1, siempre con replace.
    """
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return b.decode(enc)
        except Exception:
            continue
    return b.decode("latin-1", errors="replace")


class ClientGUI(tk.Tk):
    LOCAL_RUNNING = set()

    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("720x500")
        self.minsize(720, 500)

        self.proc = None
        self.reader_thread = None
        self.stop_read = False
        self.running_node_id = None

        self._flag_first_disk_supported = None
        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 12, "pady": 10}
        top = ttk.Frame(self)
        top.pack(fill="x", **pad)

        ttk.Label(top, text="Departamento / Nodo:").grid(row=0, column=0, sticky="w")
        self.combo = ttk.Combobox(
            top,
            state="readonly",
            width=30,
            values=[f"{n} ({i})" for n, i in DEPARTAMENTOS]
        )
        self.combo.current(0)
        self.combo.grid(row=0, column=1, sticky="w")

        ttk.Label(top, text="Server IP (fijo):").grid(row=1, column=0, sticky="w")
        ttk.Label(top, text=SERVER_IP_FIXED).grid(row=1, column=1, sticky="w")

        ttk.Label(top, text="Intervalo (s):").grid(row=2, column=0, sticky="w")
        self.int_var = tk.StringVar(value="5")
        ttk.Entry(top, textvariable=self.int_var, width=10).grid(row=2, column=1, sticky="w")

        self.first_only_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Reportar solo 1 disco (compatibilidad)", variable=self.first_only_var)\
            .grid(row=3, column=1, sticky="w")

        btns = ttk.Frame(top)
        btns.grid(row=4, column=1, sticky="w", pady=(6, 0))

        self.start_btn = ttk.Button(btns, text="Conectar / Iniciar", command=self.start_client)
        self.start_btn.pack(side="left", padx=(0, 10))

        self.stop_btn = ttk.Button(btns, text="Detener", command=self.stop_client, state="disabled")
        self.stop_btn.pack(side="left", padx=(0, 10))

        self.test_btn = ttk.Button(btns, text="Test /api/nodes", command=self.test_api_nodes)
        self.test_btn.pack(side="left")

        ttk.Separator(self).pack(fill="x", padx=12, pady=10)

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        ttk.Label(log_frame, text="Log del cliente:").pack(anchor="w")

        self.log = tk.Text(log_frame, height=20, wrap="none")
        self.log.pack(fill="both", expand=True)

        yscroll = ttk.Scrollbar(self.log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=yscroll.set)
        yscroll.pack(side="right", fill="y")

        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def append_log(self, s: str):
        self.log.insert("end", s)
        self.log.see("end")

    def selected_node_id(self) -> str:
        idx = self.combo.current()
        if idx < 0 or idx >= len(DEPARTAMENTOS):
            raise ValueError("No hay departamento seleccionado.")
        return DEPARTAMENTOS[idx][1]

    def test_api_nodes(self):
        nodes = fetch_nodes()
        if nodes is None:
            messagebox.showerror("API", f"No responde http://{WEB_HOST}:{WEB_PORT}/api/nodes\n¿Está levantado web/app.py?")
            return
        active = [n.get("node_id") for n in nodes if str(n.get("status","")).upper() == "ACTIVE"]
        messagebox.showinfo("API OK", f"API responde.\nActivos: {active}")

    def start_client(self):
        if self.proc and self.proc.poll() is None:
            messagebox.showwarning("Cliente", "Este cliente ya está en ejecución.")
            return

        try:
            node_id = self.selected_node_id()
        except Exception as e:
            messagebox.showerror("Error", str(e))
            return

        interval = self.int_var.get().strip()
        if not interval.isdigit() or int(interval) <= 0:
            messagebox.showerror("Datos inválidos", "Intervalo debe ser número entero > 0.")
            return

        if node_id in ClientGUI.LOCAL_RUNNING:
            messagebox.showerror("Nodo ocupado", f"El nodo {node_id} ya está ejecutándose en esta PC.\nElige otro.")
            return

        act = active_node_ids()
        if act is not None and node_id in act:
            messagebox.showerror("Nodo ocupado", f"El nodo {node_id} ya está ACTIVO en el servidor.\nElige otro.")
            return

        if self._flag_first_disk_supported is None:
            self._flag_first_disk_supported = client_supports_flag("--first-disk-only")

        cmd = [
            py_executable(),
            client_script_path(),
            "--client-id", node_id,
            "--server-ip", SERVER_IP_FIXED,
            "--server-port", str(SERVER_PORT),
            "--interval", interval
        ]

        if self.first_only_var.get():
            if self._flag_first_disk_supported:
                cmd.append("--first-disk-only")
            else:
                self.append_log(f"[{now_hms()}] ⚠️ client.py no soporta --first-disk-only. Se ignorará.\n")

        try:
            self.running_node_id = node_id
            ClientGUI.LOCAL_RUNNING.add(node_id)

            self.append_log(f"\n[{now_hms()}] Iniciando nodo={node_id} ip={SERVER_IP_FIXED} interval={interval}\n")
            self.append_log(f"[CMD] {' '.join(cmd)}\n")

            self.stop_read = False

            # 👇 IMPORTANTE: text=False para leer BYTES y decodificar con fallback
            self.proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=False,
                bufsize=1,
                cwd=script_dir(),
            )

            self.start_btn.config(state="disabled")
            self.stop_btn.config(state="normal")

            self.reader_thread = threading.Thread(target=self._read_output_bytes, daemon=True)
            self.reader_thread.start()

        except Exception as e:
            if self.running_node_id:
                ClientGUI.LOCAL_RUNNING.discard(self.running_node_id)
                self.running_node_id = None
            messagebox.showerror("Error", f"No se pudo iniciar el cliente: {e}")

    def _read_output_bytes(self):
        try:
            while True:
                if self.stop_read:
                    break
                b = self.proc.stdout.readline()
                if not b:
                    break
                line = decode_line(b)
                self.after(0, self.append_log, line)
        except Exception as e:
            self.after(0, self.append_log, f"\n[{now_hms()}] [ERROR leyendo salida] {e}\n")
        finally:
            code = None
            try:
                code = self.proc.poll()
            except Exception:
                pass
            self.after(0, self.append_log, f"\n[{now_hms()}] Proceso terminó. code={code}\n")
            self.after(0, self._unlock_buttons)

    def _unlock_buttons(self):
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")

        if self.running_node_id:
            ClientGUI.LOCAL_RUNNING.discard(self.running_node_id)
            self.running_node_id = None

    def stop_client(self):
        if not self.proc or self.proc.poll() is not None:
            return
        self.stop_read = True
        try:
            self.append_log(f"\n[{now_hms()}] Deteniendo cliente...\n")
            self.proc.terminate()
        except Exception:
            pass

    def on_close(self):
        try:
            self.stop_client()
        finally:
            self.destroy()


if __name__ == "__main__":
    try:
        style = ttk.Style()
        style.theme_use("clam")
    except Exception:
        pass
    app = ClientGUI()
    app.mainloop()