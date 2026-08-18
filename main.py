import socket
import threading
import json
import os
import time
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
USAGE_PATH = os.path.join(BASE_DIR, "usage.json")
FORBIDDEN_PAGE_PATH = os.path.join(BASE_DIR, "403.html")

LISTEN_HOST = "0.0.0.0"   # Escucha en todas las interfaces (para usarlo desde otros
                          # dispositivos de la red); usar "127.0.0.1" si solo se
                          # usará desde la propia Raspberry Pi
LISTEN_PORT = 8119 # Número de puerto
BUFFER_SIZE = 8192 # Tamaño de buffer
CHECK_INTERVAL = 5 # segundos entre cada actualización del contador en sesiones HTTPS

usage_lock = threading.Lock()
usage_memory = {} # Mantener estado en memoria


# ---------------------------------------------------------------------------
# Configuración y persistencia del uso diario
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def load_usage():
    """Carga el estado inicial a memoria si aún no está cargado."""
    global usage_memory
    today = date.today().isoformat()
    if os.path.exists(USAGE_PATH):
        try:
            with open(USAGE_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                if data.get("date") == today:
                    usage_memory = data
                    return usage_memory
        except (json.JSONDecodeError, OSError):
            pass

    usage_memory = {"date": today, "usage": {}}
    save_usage_to_disk(usage_memory)
    return usage_memory

def save_usage_to_disk(data):
    tmp_path = USAGE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, USAGE_PATH)

def add_used_seconds(domain, seconds):
    with usage_lock:
        today = date.today().isoformat()
        if usage_memory.get("date") != today:
            usage_memory["date"] = today
            usage_memory["usage"] = {}

        usage_memory["usage"][domain] = usage_memory["usage"].get(domain, 0) + seconds
        save_usage_to_disk(usage_memory)
        return usage_memory["usage"][domain]

def get_domain_limit(config, host):
    """Busca coincidencia exacta o de subdominio (ej. www.youtube.com -> youtube.com)."""
    for domain, limit in config.get("sites", {}).items():
        if host == domain or host.endswith("." + domain):
            return domain, limit
    return None, None


def is_blocked(host, config):
    domain, limit = get_domain_limit(config, host)
    if domain is None:
        return False, None, None
    with usage_lock:
        data = load_usage()
        used = data["usage"].get(domain, 0)
    return used >= limit, domain, limit


# ---------------------------------------------------------------------------
# Parseo de peticiones
# ---------------------------------------------------------------------------

def parse_request(data):
    """Devuelve (metodo, host, puerto) a partir de los bytes crudos de la petición."""
    try:
        first_line = data.split(b"\r\n", 1)[0].decode("utf-8", errors="ignore")
        method, target, *_ = first_line.split(" ")
    except (ValueError, IndexError):
        return None, None, None

    if method == "CONNECT":
        # target tiene forma "host:puerto" (HTTPS)
        host, _, port = target.partition(":")
        return method, host, int(port) if port else 443

    # Petición HTTP normal: el host viene en la cabecera "Host:"
    host, port = None, 80
    for line in data.split(b"\r\n")[1:]:
        if line.lower().startswith(b"host:"):
            host_header = line.split(b":", 1)[1].strip().decode("utf-8", errors="ignore")
            if ":" in host_header:
                host, port_str = host_header.split(":", 1)
                port = int(port_str)
            else:
                host = host_header
            break
    return method, host, port


# ---------------------------------------------------------------------------
# Respuestas de bloqueo
# ---------------------------------------------------------------------------

def send_forbidden_http(client_socket):
    try:
        with open(FORBIDDEN_PAGE_PATH, "r", encoding="utf-8") as f:
            body = f.read()
    except FileNotFoundError:
        body = "<h1>403 - Acceso prohibido</h1>"
    body_bytes = body.encode("utf-8")
    response = (
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body_bytes)).encode() + b"\r\n"
        b"Connection: close\r\n\r\n" + body_bytes
    )
    client_socket.sendall(response)


# ---------------------------------------------------------------------------
# Manejo de HTTP (sin cifrar)
# ---------------------------------------------------------------------------

def handle_http(client_socket, request, host, port, domain):
    try:
        destination_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        destination_socket.settimeout(10)
        destination_socket.connect((host, port))
        destination_socket.sendall(request)
    except OSError:
        client_socket.close()
        return

    start = time.time()
    try:
        while True:
            data = destination_socket.recv(BUFFER_SIZE)
            if not data:
                break
            client_socket.sendall(data)
    except OSError:
        pass
    finally:
        elapsed = int(time.time() - start)
        if domain is not None and elapsed > 0:
            add_used_seconds(domain, elapsed)
        destination_socket.close()
        client_socket.close()


# ---------------------------------------------------------------------------
# Manejo de HTTPS (CONNECT / túnel)
# ---------------------------------------------------------------------------

def relay(source, destination, stop_event):
    """Copia datos de source a destination hasta que se corte la conexión."""
    try:
        source.settimeout(1.0)
        while not stop_event.is_set():
            try:
                data = source.recv(BUFFER_SIZE)
            except socket.timeout:
                continue
            if not data:
                break
            destination.sendall(data)
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        stop_event.set()


def monitor_https_session(client_socket, destination_socket, domain, limit, stop_event):
    """Va sumando tiempo de uso mientras el túnel HTTPS está abierto y lo
    corta en cuanto se supera el límite diario."""
    while not stop_event.is_set():
        time.sleep(CHECK_INTERVAL)
        used_total = add_used_seconds(domain, CHECK_INTERVAL)
        if used_total >= limit:
            print(f"[BLOQUEADO] {domain} superó el límite ({used_total}s >= {limit}s).")
            stop_event.set()
            break
    for sock in (client_socket, destination_socket):
        try:
            sock.close()
        except OSError:
            pass


def handle_https(client_socket, host, port, domain, limit):
    try:
        destination_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        destination_socket.settimeout(10)
        destination_socket.connect((host, port))
    except OSError:
        client_socket.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
        client_socket.close()
        return

    client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")

    stop_event = threading.Event()
    threads = [
        threading.Thread(target=relay, args=(client_socket, destination_socket, stop_event)),
        threading.Thread(target=relay, args=(destination_socket, client_socket, stop_event)),
    ]
    if domain is not None:
        threads.append(threading.Thread(
            target=monitor_https_session,
            args=(client_socket, destination_socket, domain, limit, stop_event),
        ))

    for t in threads:
        t.start()
    for t in threads:
        t.join()


# ---------------------------------------------------------------------------
# Bucle principal
# ---------------------------------------------------------------------------

def handle_client(client_socket, config):
    try:
        request = client_socket.recv(BUFFER_SIZE)
    except OSError:
        client_socket.close()
        return
    if not request:
        client_socket.close()
        return

    method, host, port = parse_request(request)
    if host is None:
        client_socket.close()
        return

    blocked, domain, limit = is_blocked(host, config)
    if blocked:
        print(f"[BLOQUEADO] {host} ya superó su límite diario.")
        if method == "CONNECT":
            # No se puede servir 403.html dentro de un túnel HTTPS sin hacer
            # MITM con un certificado propio instalado en el cliente.
            client_socket.sendall(b"HTTP/1.1 403 Forbidden\r\n\r\n")
        else:
            send_forbidden_http(client_socket)
        client_socket.close()
        return

    if method == "CONNECT":
        handle_https(client_socket, host, port, domain, limit)
    else:
        handle_http(client_socket, request, host, port, domain)


def start_proxy_server():
    config = load_config()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(50)
    print(f"Proxy escuchando en {LISTEN_HOST}:{LISTEN_PORT}...")
    while True:
        client_socket, _addr = server.accept()
        threading.Thread(target=handle_client, args=(client_socket, config), daemon=True).start()


if __name__ == "__main__":
    start_proxy_server()
