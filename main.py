import socket
import threading
import json
import time
import os

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8119
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "config.json")
USAGE_FILE = os.path.join(os.path.dirname(__file__), "usage.json")
HTML_403_FILE = os.path.join(os.path.dirname(__file__), "403.html")
CURRENT_CONFIG = {}

threading.stack_size(512 * 1024)

def config_reloader(interval=60):
    """Hilo en segundo plano que recarga config.json cada 'interval' segundos."""
    global CURRENT_CONFIG
    while True:
        try:
            new_config = load_config()
            CURRENT_CONFIG = new_config
        except Exception as e:
            print(f"[ERROR] No se pudo recargar config.json: {e}")
        time.sleep(interval)

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[ERROR] Error al cargar config.json: {e}")
    return {"default_time_limit_seconds": 3600, "domain_limits": {}}

def get_today_key():
    return time.strftime("%Y-%m-%d")

def match_domain(target_host, domain_limits):
    """Encuentra si el host coincide con algún dominio configurado (ej: www.youtube.com -> youtube.com)"""
    target_host = target_host.lower().split(":")[0]
    for domain in domain_limits:
        if target_host == domain or target_host.endswith("." + domain):
            return domain
    return None

def get_used_time(domain):
    today = get_today_key()
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, "r") as f:
                data = json.load(f)
                return data.get(today, {}).get(domain, 0)
        except Exception:
            return 0
    return 0

def add_used_time(domain, seconds):
    today = get_today_key()
    data = {}
    if os.path.exists(USAGE_FILE):
        try:
            with open(USAGE_FILE, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    
    if today not in data:
        data[today] = {}
        
    current = data[today].get(domain, 0)
    data[today][domain] = current + seconds
    
    try:
        with open(USAGE_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[ERROR] No se pudo guardar usage.json: {e}")

def get_403_response():
    if os.path.exists(HTML_403_FILE):
        with open(HTML_403_FILE, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = "<h1>403 Límite de tiempo alcanzado para este sitio</h1>"
    
    response = (
        "HTTP/1.1 403 Forbidden\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(content.encode('utf-8'))}\r\n"
        "Connection: close\r\n\r\n" + content
    )
    return response.encode('utf-8')

def relay(src, dst):
    try:
        while True:
            data = src.recv(4096)
            if not data:
                break
            dst.sendall(data)
    except Exception:
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass
        try:
            dst.close()
        except Exception:
            pass

def handle_client(client_socket):
    try:
        # Usar la configuración global recargada automáticamente
        config = CURRENT_CONFIG
        
        client_socket.settimeout(60.0)
        request = client_socket.recv(4096)
        if not request:
            client_socket.close()
            return

        lines = request.decode('latin-1', errors='ignore').split('\r\n')
        if not lines or len(lines[0].split(' ')) < 2:
            client_socket.close()
            return
            
        first_line = lines[0]
        method, url, _ = first_line.split(' ', 2)
        
        # Extraer host destino
        if method == "CONNECT":
            host = url.split(":")[0]
            port = int(url.split(":")[1]) if ":" in url else 443
        else:
            host = ""
            for line in lines:
                if line.lower().startswith("host:"):
                    host = line.split(":", 1)[1].strip().split(":")[0]
                    break
            if not host:
                host = url.split('/')[2] if '://' in url else url.split('/')[0]
            port = 80

        # Determinar límites de tiempo para este host
        domain_limits = config.get("domain_limits", {})
        matched_domain = match_domain(host, domain_limits)
        
        if matched_domain:
            limit = domain_limits[matched_domain]
            tracked_domain = matched_domain
        else:
            limit = config.get("default_time_limit_seconds", 3600)
            tracked_domain = "global"

        # Comprobar tiempo consumido
        used = get_used_time(tracked_domain)
        if used >= limit:
            client_socket.sendall(get_403_response())
            client_socket.close()
            return

        # Establecer conexión con destino
        dest_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dest_socket.settimeout(10.0)
        dest_socket.connect((host, port))
        dest_socket.settimeout(60.0)

        start_time = time.time()

        if method == "CONNECT":
            client_socket.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        else:
            dest_socket.sendall(request)

        t1 = threading.Thread(target=relay, args=(client_socket, dest_socket), daemon=True)
        t2 = threading.Thread(target=relay, args=(dest_socket, client_socket), daemon=True)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        elapsed = time.time() - start_time
        if elapsed > 0:
            add_used_time(tracked_domain, int(elapsed))

    except Exception:
        pass
    finally:
        try:
            client_socket.close()
        except Exception:
            pass

def start_proxy_server():
    global CURRENT_CONFIG
    CURRENT_CONFIG = load_config()
    
    # Iniciar hilo de recarga automática en segundo plano (cada 60 segundos)
    reloader_thread = threading.Thread(target=config_reloader, args=(60,), daemon=True)
    reloader_thread.start()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.settimeout(None)
    
    server.bind((LISTEN_HOST, LISTEN_PORT))
    server.listen(50)
    print(f"Proxy escuchando en {LISTEN_HOST}:{LISTEN_PORT} (recarga de config activa)...")
    
    while True:
        try:
            client_socket, _addr = server.accept()
            t = threading.Thread(
                target=handle_client, 
                args=(client_socket,), 
                daemon=True
            )
            t.start()
        except Exception as e:
            print(f"[ERROR] Error aceptando cliente: {e}")

if __name__ == "__main__":
    start_proxy_server()
