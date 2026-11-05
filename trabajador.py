#!/usr/bin/env python3
"""Trabajador de la red agentica: conecta TU Claude Code (tu cuenta personal) a la cola de tareas.

Bucle: latido -> tomar tarea -> carpeta aislada (clona el repo del proyecto si hay) -> leer la conversacion del
proyecto -> `claude -p` -> publicar mensajes que Claude dejo -> terminar tarea (o devolverla si llegaste al limite).

Uso:  python trabajador.py               (lee trabajador.json junto al script)
      python trabajador.py --una-vez     (toma como mucho una tarea y sale; util para rutinas/cron)
Solo biblioteca estandar. Requiere `claude` (Claude Code) instalado y con sesion iniciada, y `git` si los proyectos
tienen repo. Opcional: graphify (`pip install graphifyy`) para mantener el mapa del codigo.
"""
import argparse, datetime as dt, json, os, re, shutil, subprocess, sys, threading, time, urllib.request, urllib.error

AQUI = os.path.dirname(os.path.abspath(__file__))
CFG = {
    "url": "https://sjlrrsowdxlpipgvfywa.supabase.co",
    "clave_publica": "sb_publishable_AXnicGWc6hWdXOjiZ2-d9g_2j2tyBHy",   # clave publica de la pagina (no es secreta)
    "agente_id": "",              # "Mis agentes" en la pagina
    "llave": "",                  # boton "Generar llave" en la pagina (solo se muestra una vez)
    "carpeta_trabajo": os.path.join(AQUI, "trabajo"),
    "max_tareas_dia": 10,
    "horario": {"desde": "00:00", "hasta": "23:59"},   # hora local en que presto el cupo
    "espera_sin_tareas_s": 300,
    "espera_limite_s": 1800,
    "minutos_por_tarea": 90,
    # Permisos de Claude dentro de la carpeta de la tarea. Conservador por defecto: leer/editar archivos y git.
    "claude_args": ["--permission-mode", "acceptEdits",
                    "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash(git status:*),Bash(git diff:*),Bash(git log:*),Bash(graphify query:*),Bash(graphify explain:*),Bash(graphify path:*)"],
}
RE_LIMITE = re.compile(r"usage limit|rate limit|limit reached|5-hour limit|weekly limit|limite de uso", re.I)


def anotar(*a):
    print(dt.datetime.now().strftime("%d-%m %H:%M:%S"), *a, flush=True)


def rpc(fn, **args):
    req = urllib.request.Request(f"{CFG['url']}/rest/v1/rpc/{fn}", data=json.dumps(args).encode(), method="POST",
                                 headers={"apikey": CFG["clave_publica"], "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            cuerpo = r.read().decode() or "null"
            return json.loads(cuerpo)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{fn}: {e.code} {e.read().decode()[:300]}") from None


def cred():
    return {"p_agente": CFG["agente_id"], "p_llave": CFG["llave"]}


def en_horario():
    ahora = dt.datetime.now().strftime("%H:%M")
    d, h = CFG["horario"]["desde"], CFG["horario"]["hasta"]
    return d <= ahora <= h if d <= h else (ahora >= d or ahora <= h)


class Latido(threading.Thread):
    """Late cada 5 min mientras Claude trabaja; si el dueno pausa desde la pagina, marca self.pausa."""
    def __init__(self):
        super().__init__(daemon=True); self.fin = threading.Event(); self.pausa = False
    def run(self):
        while not self.fin.wait(300):
            try:
                if rpc("t_latido", **cred(), p_estado="trabajando") is False:
                    self.pausa = True
            except Exception as e:
                anotar("latido fallo:", e)


IGNORAR_DIRS = {".git", "graphify-out", "NOTAS", "node_modules", "__pycache__", ".venv", "venv", ".claude"}
IGNORAR_ARCH = {"TAREA.md", "RESULTADO.md", "MENSAJES.txt", "SUBTAREAS.json"}
MAX_BYTES = 1_000_000


class Sincronizador(threading.Thread):
    """Codigo en vivo: sube cada archivo que cambia (a los segundos) y aplica los cambios de los demas.
    Si dos editan el mismo archivo a la vez, el servidor guarda el segundo como conflicto (no pisa)."""

    def __init__(self, base, proyecto_id, tarea_id, intervalo=2.5):
        super().__init__(daemon=True)
        self.base, self.pid, self.tid, self.intervalo = base, proyecto_id, tarea_id, intervalo
        self.fin = threading.Event()
        self.ver = {}        # ruta -> version del servidor que tengo
        self.sync = {}       # ruta -> sha de lo ultimo sincronizado
        self.stat = {}       # ruta -> (mtime, size) para no releer archivos sin cambios
        self.conflictos = set()
        self.desde = None
        self.subidos = self.bajados = 0

    @staticmethod
    def sha(texto):
        import hashlib
        return hashlib.sha256(texto.encode("utf-8")).hexdigest()

    def candidatos(self):
        if os.path.isdir(os.path.join(self.base, ".git")):   # respeta .gitignore
            r = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=self.base,
                               capture_output=True, text=True, encoding="utf-8", errors="replace")
            rutas = [x for x in r.stdout.split("\0") if x]
        else:
            rutas = []
            for raiz, dirs, archs in os.walk(self.base):
                dirs[:] = [d for d in dirs if d not in IGNORAR_DIRS]
                rutas += [os.path.relpath(os.path.join(raiz, a), self.base).replace(os.sep, "/") for a in archs]
        return [r for r in rutas if r.split("/")[0] not in IGNORAR_DIRS and r not in IGNORAR_ARCH]

    def leer(self, ruta):
        f = os.path.join(self.base, ruta)
        try:
            if os.path.getsize(f) > MAX_BYTES:
                return None
            datos = open(f, "rb").read()
            if b"\0" in datos[:8000]:
                return None                                    # binario: no viaja por la red
            return datos.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def remotos(self, desde=None, contenido=False):
        filas, off = [], 0
        while True:
            lote = rpc("t_archivos", **cred(), p_proyecto=self.pid, p_desde=desde, p_con_contenido=contenido, p_offset=off, p_limite=200) or []
            filas += lote; off += len(lote)
            if len(lote) < 200:
                return filas

    def escribir(self, fila):
        f = os.path.join(self.base, fila["ruta"])
        if fila["borrado"]:
            if os.path.exists(f):
                os.remove(f)
            self.sync.pop(fila["ruta"], None)
        else:
            os.makedirs(os.path.dirname(f) or self.base, exist_ok=True)
            open(f, "w", encoding="utf-8", newline="").write(fila["contenido"])
            self.sync[fila["ruta"]] = fila["sha"]
            st = os.stat(f); self.stat[fila["ruta"]] = (st.st_mtime, st.st_size)
        self.ver[fila["ruta"]] = fila["version"]

    def subir(self, ruta, texto, borrar=False):
        r = rpc("t_archivo_subir", **cred(), p_proyecto=self.pid, p_ruta=ruta, p_contenido=texto or "",
                p_base=self.ver.get(ruta, 0), p_tarea=self.tid, p_borrar=borrar)
        if r.get("conflicto"):
            if ruta not in self.conflictos:
                anotar("CONFLICTO en", ruta, "(otro lo cambio a la vez; quedo guardado aparte)")
            self.conflictos.add(ruta)
            return
        self.ver[ruta] = r["version"]; self.conflictos.discard(ruta); self.subidos += 1
        if borrar:
            self.sync.pop(ruta, None)
        else:
            self.sync[ruta] = self.sha(texto)

    def iniciar(self):
        """Baja el espacio central; si esta vacio y hay repo clonado, lo siembra con los archivos de texto."""
        remotos = self.remotos(contenido=True)
        if remotos:
            for fila in remotos:
                self.escribir(fila)
            self.desde = max(f["actualizado"] for f in remotos)
            anotar(f"codigo en vivo: {len(remotos)} archivos del espacio central")
        else:
            rutas = self.candidatos()[:3000]
            for ruta in rutas:
                texto = self.leer(ruta)
                if texto is not None:
                    self.subir(ruta, texto)
            anotar(f"codigo en vivo: espacio central sembrado con {self.subidos} archivos")
            self.desde = max((f["actualizado"] for f in self.remotos()), default=None)
        for ruta in self.candidatos():
            f = os.path.join(self.base, ruta)
            if os.path.exists(f):
                st = os.stat(f); self.stat[ruta] = (st.st_mtime, st.st_size)
                if ruta not in self.sync:
                    texto = self.leer(ruta)
                    if texto is not None:
                        self.sync[ruta] = self.sha(texto)

    def ciclo(self):
        # 1) mis cambios locales -> servidor
        vistos = set()
        for ruta in self.candidatos():
            vistos.add(ruta)
            f = os.path.join(self.base, ruta)
            try:
                st = os.stat(f)
            except OSError:
                continue
            if self.stat.get(ruta) == (st.st_mtime, st.st_size):
                continue
            self.stat[ruta] = (st.st_mtime, st.st_size)
            texto = self.leer(ruta)
            if texto is not None and self.sync.get(ruta) != self.sha(texto):
                self.subir(ruta, texto)
        for ruta in [r for r in self.sync if r not in vistos and not os.path.exists(os.path.join(self.base, r))]:
            self.subir(ruta, "", borrar=True)
        # 2) cambios de los demas -> mi carpeta (si yo no tengo cambios sin subir en ese archivo)
        for fila in self.remotos(desde=self.desde, contenido=True):
            self.desde = max(self.desde or fila["actualizado"], fila["actualizado"])
            if fila["version"] <= self.ver.get(fila["ruta"], 0):
                continue                                      # eco de mi propio cambio
            local = self.leer(fila["ruta"])
            if local is None or self.sync.get(fila["ruta"]) in (None, self.sha(local)):
                self.escribir(fila); self.bajados += 1
            else:
                self.ver[fila["ruta"]] = self.ver.get(fila["ruta"], 0)   # mi subida chocara -> conflicto visible

    def run(self):
        while not self.fin.wait(self.intervalo):
            try:
                self.ciclo()
            except Exception as e:
                anotar("sincronizacion fallo:", e)

    def terminar(self):
        self.fin.set(); self.join(timeout=30)
        try:
            self.ciclo()                                      # ultimo empujon
        except Exception as e:
            anotar("sincronizacion final fallo:", e)
        anotar(f"codigo en vivo: {self.subidos} subidas, {self.bajados} bajadas, {len(self.conflictos)} conflictos")


def preparar_carpeta(t, proyecto_repo):
    base = os.path.join(CFG["carpeta_trabajo"], t["id"])
    shutil.rmtree(base, ignore_errors=True); os.makedirs(base)
    if proyecto_repo:
        r = subprocess.run(["git", "clone", "--depth", "50", proyecto_repo, base], capture_output=True, text=True)
        if r.returncode != 0:
            anotar("no pude clonar", proyecto_repo, r.stderr[-300:]); os.makedirs(base, exist_ok=True)
    return base


def armar_prompt(t, mensajes, notas):
    lista_notas = "\n".join(f"- {n['ruta']}" for n in (notas or [])) or "(sin notas todavia)"
    conv = "\n".join(f"- [{m['ts'][:16]}] {m['de']}: {m['texto']}" for m in reversed(mensajes or [])) or "(sin mensajes)"
    return f"""Eres un agente de la red agentica comunitaria trabajando en el proyecto de otra persona, con el cupo de tu dueno.
Trabaja SOLO dentro de esta carpeta. No subas credenciales ni datos personales.

# Tarea: {t['titulo']}
{t['descripcion']}

# Conversacion reciente del proyecto (otros agentes y personas)
{conv}

# Conocimiento del proyecto
- Si existe graphify-out/GRAPH_REPORT.md, LEELO PRIMERO: es el mapa del codigo y ahorra tokens. Para preguntas puntuales:
  `graphify query "<pregunta>"`, `graphify explain "<nodo>"`, `graphify path "A" "B"`.
- Notas del proyecto (vault de Obsidian), copiadas en NOTAS/:
{lista_notas}
- Si aprendes algo que sirva a otros (decision, hallazgo, como funciona algo), crea o actualiza una nota en NOTAS/
  (markdown, una idea por nota, enlaza otras notas con [[Nombre de la nota]]).

# Como entregar
- Deja tu resultado final en RESULTADO.md (resumen claro de lo que hiciste, hallazgos y siguientes pasos).
- Si quieres decirle algo al resto (coordinar, avisar un hallazgo, pedir algo), escribe una linea por mensaje en
  MENSAJES.txt. Formato: `todos: texto` o `<id_agente>: texto` para un agente en particular.
- CODIGO EN VIVO: cada archivo que guardes se comparte a los segundos con el resto de la red, y los cambios de otros
  agentes aparecen solos en tu carpeta. Haz cambios chicos y coherentes; antes de editar un archivo, vuelve a leerlo.
  Si otro lo cambio a la vez, tu version queda como conflicto (no se pierde): mencionalo en RESULTADO.md.
"""


def publicar_mensajes(base, proyecto_id):
    f = os.path.join(base, "MENSAJES.txt")
    if not os.path.exists(f):
        return 0
    n = 0
    for linea in open(f, encoding="utf-8", errors="replace"):
        if ":" not in linea.strip():
            continue
        para, texto = linea.split(":", 1)
        para = para.strip()
        para = None if para.lower() in ("todos", "all", "") else para
        try:
            rpc("t_mensaje", **cred(), p_proyecto=proyecto_id, p_texto=texto.strip(), p_para=para); n += 1
        except Exception as e:
            anotar("mensaje no enviado:", e)
    return n


def graphify(base):
    """Actualiza el mapa del codigo (solo AST, sin gastar tokens) si graphify esta instalado y hay codigo."""
    if not shutil.which("graphify") or not os.path.isdir(os.path.join(base, ".git")):
        return False
    r = subprocess.run(["graphify", "update", "."], cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
    return r.returncode == 0 and os.path.exists(os.path.join(base, "graphify-out", "graph.json"))


def grafo_compacto(ruta):
    g = json.load(open(ruta, encoding="utf-8"))
    nodos = [{k: n.get(k) for k in ("id", "label", "community", "community_name", "source_file")} for n in g.get("nodes", [])]
    enlaces = [{"source": e["source"], "target": e["target"], "relation": e.get("relation")} for e in g.get("links", g.get("edges", []))]
    return json.dumps({"nodes": nodos, "links": enlaces, "commit": g.get("built_at_commit")}, ensure_ascii=False, separators=(",", ":"))


def subir_conocimiento(base, proyecto_id, notas_antes):
    """Sube el mapa de graphify (si se actualizo) y las notas nuevas o cambiadas de NOTAS/."""
    subidos = 0
    if graphify(base):
        go = os.path.join(base, "graphify-out")
        try:
            rpc("t_conocimiento", **cred(), p_proyecto=proyecto_id, p_ruta="codigo/GRAPH_REPORT.md", p_tipo="reporte",
                p_contenido=open(os.path.join(go, "GRAPH_REPORT.md"), encoding="utf-8", errors="replace").read())
            rpc("t_conocimiento", **cred(), p_proyecto=proyecto_id, p_ruta="codigo/grafo.json", p_tipo="grafo",
                p_contenido=grafo_compacto(os.path.join(go, "graph.json")))
            subidos += 2
        except Exception as e:
            anotar("no pude subir el mapa:", e)
    carpeta = os.path.join(base, "NOTAS")
    for nombre in sorted(os.listdir(carpeta)) if os.path.isdir(carpeta) else []:
        if not nombre.endswith(".md"):
            continue
        texto = open(os.path.join(carpeta, nombre), encoding="utf-8", errors="replace").read()
        ruta = "notas/" + nombre
        if notas_antes.get(ruta) == texto:
            continue
        try:
            rpc("t_conocimiento", **cred(), p_proyecto=proyecto_id, p_ruta=ruta, p_tipo="nota", p_contenido=texto); subidos += 1
        except Exception as e:
            anotar("nota no subida:", nombre, e)
    if subidos:
        anotar(f"conocimiento: {subidos} archivo(s) subidos")


def ejecutar_claude(prompt, base, sesion=None):
    """Corre Claude Code sin pantalla. Devuelve dict con texto, sesion, tokens, costo, codigo, crudo."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", *CFG["claude_args"]]
    if sesion:
        cmd += ["--resume", sesion]
    try:
        r = subprocess.run(cmd, cwd=base, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=CFG["minutos_por_tarea"] * 60)
        crudo, codigo = (r.stdout or "") + (r.stderr or ""), r.returncode
    except subprocess.TimeoutExpired:
        return dict(texto="se acabo el tiempo de la tarea", sesion=sesion, tokens=0, costo=None, codigo=-1, crudo="")
    try:
        j = json.loads(r.stdout)
        u = j.get("usage") or {}
        tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
        return dict(texto=j.get("result") or "", sesion=j.get("session_id") or sesion, tokens=tokens,
                    costo=j.get("total_cost_usd"), codigo=1 if j.get("is_error") else codigo, crudo=crudo)
    except (ValueError, TypeError):
        return dict(texto=crudo[-20000:], sesion=sesion, tokens=0, costo=None, codigo=codigo, crudo=crudo)


def registrar_consumo(t, r):
    if r["tokens"]:
        try:
            rpc("t_consumo", **cred(), p_tarea=t["id"], p_tokens=r["tokens"], p_costo=r["costo"])
        except Exception as e:
            anotar("no pude registrar consumo:", e)


def ajuste_de_nota(t):
    """Si la mision nace de una nota guardada, arma la seccion con lo que cambio."""
    ruta = (t.get("config") or {}).get("nota")
    if not ruta:
        return ""
    try:
        filas = rpc("t_ajuste_nota", **cred(), p_proyecto=t["proyecto_id"], p_ruta=ruta) or []
    except Exception:
        filas = []
    if not filas:
        return ""
    import difflib
    f = filas[0]
    diff = "\n".join(difflib.unified_diff((f["antes"] or "").splitlines(), f["despues"].splitlines(), "antes", "despues", lineterm=""))
    return f"\n# Ajuste de la nota {ruta} (por {f['quien']}, {f['ts'][:16]})\n```diff\n{diff or f['despues']}\n```\n"


def prompt_modo(t, companeros):
    if t.get("modo") == "opinion":
        return """
# Te piden tu OPINION
Responde breve y concreto en RESULTADO.md (que harias, riesgos, alternativas). No cambies codigo salvo que te lo pidan.
"""
    if t.get("modo") != "mision":
        return ""
    lista = "\n".join(f"- {c['id']}: {c['nombre']} (de {c['dueno']}, {'en linea' if c['vivo'] else 'desconectado'}, {c['estado'] or '-'})"
                      for c in companeros) or "(nadie mas presta a este proyecto ahora: haz el trabajo tu mismo)"
    return f"""
# Eres el ORQUESTADOR de esta mision (trabajas COLABORATIVAMENTE con otros Claude de la red)
Agentes disponibles:
{lista}
- Planifica. Si conviene, PIDE OPINIONES y DELEGA partes del trabajo escribiendo SUBTAREAS.json (lista):
  [{{"titulo": "...", "descripcion": "instrucciones claras y autocontenidas", "modo": "opinion" o "trabajo", "para": "<id de agente o null>"}}]
  Luego termina tu turno: el sistema esperara sus resultados y te los traera en un nuevo mensaje.
- Delega piezas que no choquen (archivos distintos). El codigo esta en vivo: veras sus cambios en tu carpeta.
- Haz tu mismo lo que sea mas rapido que explicarlo. Usa MENSAJES.txt para coordinar.
- Cuando todo este integrado, escribe RESULTADO.md (que se hizo, quien hizo que, que falta) y NO escribas SUBTAREAS.json.
"""


def leer_subtareas(base):
    f = os.path.join(base, "SUBTAREAS.json")
    if not os.path.exists(f):
        return []
    try:
        datos = json.load(open(f, encoding="utf-8"))
    except ValueError:
        datos = []
    os.remove(f)
    return [d for d in datos if isinstance(d, dict) and d.get("titulo") and d.get("descripcion")][:8]


def esperar_subtareas(t, ids, lat, max_min=45):
    """Espera a que las subtareas delegadas terminen (o se acabe el tiempo). Devuelve el estado de todas."""
    fin = time.time() + max_min * 60
    while True:
        estado = rpc("t_mision", **cred(), p_padre=t["id"]) or []
        mias = [e for e in estado if e["id"] in ids]
        if all(e["estado"] in ("hecha", "fallida", "cancelada") for e in mias) or time.time() > fin or lat.pausa:
            return estado
        time.sleep(30)


def hacer(t):
    repo = (t.get("config") or {}).get("repo_url")
    base = preparar_carpeta(t, repo)
    sinc = Sincronizador(base, t["proyecto_id"], t["id"])
    try:
        sinc.iniciar()
    except Exception as e:
        anotar("codigo en vivo no disponible:", e); sinc = None
    try:
        mensajes = rpc("t_mensajes", **cred(), p_proyecto=t["proyecto_id"])
    except Exception:
        mensajes = []
    try:
        notas = rpc("t_notas", **cred(), p_proyecto=t["proyecto_id"]) or []
    except Exception:
        notas = []
    companeros = []
    if t.get("modo") == "mision":
        try:
            companeros = rpc("t_companeros", **cred(), p_proyecto=t["proyecto_id"]) or []
        except Exception:
            pass
    os.makedirs(os.path.join(base, "NOTAS"), exist_ok=True)
    for n in notas:
        if n["ruta"].startswith("notas/"):
            destino = os.path.join(base, "NOTAS", os.path.basename(n["ruta"]))
            open(destino, "w", encoding="utf-8").write(n["contenido"])
    graphify(base)
    open(os.path.join(base, "TAREA.md"), "w", encoding="utf-8").write(f"# {t['titulo']}\n\n{t['descripcion']}\n")
    lat = Latido(); lat.start()
    if sinc:
        sinc.start()
    anotar("trabajando en", t["titulo"], f"[{t.get('modo', 'trabajo')}]", "->", base)
    prompt = armar_prompt(t, mensajes, [n for n in notas if n["ruta"].startswith("notas/")]) + ajuste_de_nota(t) + prompt_modo(t, companeros)
    r, delegadas = None, []
    try:
        for ronda in range(6):                       # mision: planear -> delegar -> esperar -> integrar (hasta 6 rondas)
            r = ejecutar_claude(prompt, base, r["sesion"] if r else None)
            registrar_consumo(t, r)
            if RE_LIMITE.search(r["crudo"]) or lat.pausa or t.get("modo") != "mision":
                break
            subs = leer_subtareas(base)
            if not subs:
                break
            ids = []
            for sbt in subs:
                try:
                    ids.append(rpc("t_subtarea", **cred(), p_padre=t["id"], p_titulo=sbt["titulo"], p_descripcion=sbt["descripcion"],
                                   p_modo=sbt.get("modo") or "trabajo", p_para=sbt.get("para") or None))
                except Exception as e:
                    anotar("no pude delegar:", sbt["titulo"], e)
            delegadas += ids
            anotar(f"ronda {ronda + 1}: delegue {len(ids)} subtarea(s); espero resultados")
            estado = esperar_subtareas(t, set(ids), lat)
            informe = "\n\n".join(f"## {e['titulo']} [{e['modo']}] -> {e['estado']} ({e['agente'] or 'sin tomar'})\n{(e['resultado'] or '')[:6000]}" for e in estado)
            prompt = f"Resultados de las subtareas que delegaste (las no terminadas siguen abiertas):\n\n{informe}\n\n" \
                     "Integra, revisa los cambios de codigo que llegaron a tu carpeta y decide: delegar mas (SUBTAREAS.json) o cerrar con RESULTADO.md."
    finally:
        lat.fin.set()
        if sinc:
            sinc.terminar()
    if RE_LIMITE.search(r["crudo"]) and not os.path.exists(os.path.join(base, "RESULTADO.md")):
        rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo="limite_uso")
        anotar("limite de uso: devuelta la tarea"); return "limite"
    if lat.pausa:
        rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo="pausa"); anotar("pausa pedida por el dueno"); return "pausa"
    n = publicar_mensajes(base, t["proyecto_id"])
    subir_conocimiento(base, t["proyecto_id"], {x["ruta"]: x["contenido"] for x in notas})
    f = os.path.join(base, "RESULTADO.md")
    resultado = open(f, encoding="utf-8", errors="replace").read() if os.path.exists(f) else r["texto"][-20000:]
    ok = r["codigo"] == 0 and os.path.exists(f)
    rpc("t_terminar", **cred(), p_tarea=t["id"], p_ok=ok, p_resultado=resultado)
    anotar("tarea", "hecha" if ok else "fallida", f"({n} mensajes, {len(delegadas)} delegadas, {r['tokens']} tokens ultima ronda)")
    return "ok"


BANDEJA = os.path.join(AQUI, "bandeja.json")


def responder_bandeja():
    """Contesta los mensajes dirigidos a este agente (chat directo desde la pagina), aunque no tenga tarea."""
    estado = json.load(open(BANDEJA, encoding="utf-8")) if os.path.exists(BANDEJA) else {}
    if time.time() < estado.get("pausa_hasta", 0):
        return 0                                   # en limite de uso: reintenta mas tarde (el aviso ya se mando)
    desde = estado.get("desde") or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat()
    try:
        msgs = rpc("t_bandeja", **cred(), p_desde=desde) or []
    except Exception as e:
        anotar("bandeja no disponible:", e); return 0
    guardar = lambda: json.dump(estado, open(BANDEJA, "w", encoding="utf-8"))
    for m in msgs:
        carpeta = os.path.join(CFG["carpeta_trabajo"], "_chat"); os.makedirs(carpeta, exist_ok=True)
        prompt = (f"Eres un agente de la Red agentica. {m['de']} te escribio directamente"
                  f"{' en el proyecto ' + m['proyecto'] if m.get('proyecto') else ''}: «{m['texto']}». "
                  "Responde breve, claro y en espanol chileno. Si te pide trabajo de codigo, explica que lo haras si lo crea como "
                  "tarea o mision en la mesa del proyecto (o si te nombra orquestador), y propone como dividirlo. "
                  "Responde SOLO con el texto del mensaje.")
        try:
            r = subprocess.run(["claude", "-p", prompt, "--output-format", "json"], cwd=carpeta, capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=300)
            texto = (json.loads(r.stdout).get("result") or "").strip()
        except Exception as e:
            texto = ""; anotar("no pude responder:", e)
        quien = m["de"].split(" (agente")[0]
        if RE_LIMITE.search(texto) or not texto:
            # no marca el mensaje como leido: lo contesta de verdad cuando se libere el limite; avisa UNA vez por mensaje
            if m["id"] not in estado.get("avisados", []):
                rpc("t_mensaje", **cred(), p_proyecto=m.get("proyecto_id"),
                    p_texto=f"@{quien}: ahora estoy en mi limite de uso; te contesto apenas se libere.", p_para=None)
                estado["avisados"] = (estado.get("avisados", []) + [m["id"]])[-50:]
            estado["pausa_hasta"] = time.time() + 1800; guardar()
            anotar("bandeja: en limite de uso, reintento en 30 min"); return 0
        rpc("t_mensaje", **cred(), p_proyecto=m.get("proyecto_id"), p_texto=f"@{quien}: {texto}"[:4000], p_para=None)
        estado["desde"] = m["ts"]; guardar()
        anotar("respondi un mensaje directo de", quien)
    return len(msgs)


def dormir_atento(segundos):
    """Espera, pero cada 30 s revisa si alguien le escribio al agente y le contesta."""
    fin = time.time() + segundos
    while time.time() < fin:
        responder_bandeja()
        time.sleep(min(30, max(1, fin - time.time())))


def uso_semanal_local():
    """Tokens usados por ESTA cuenta de Claude Code en los ultimos 7 dias, leidos de sus registros locales
    (~/.claude*/projects/**/*.jsonl). Es una estimacion: Claude no informa el % de cuota que queda."""
    import glob
    desde = time.time() - 7 * 86400
    total = 0
    # la cuenta que usa `claude`: CLAUDE_CONFIG_DIR si esta definida, si no ~/.claude (se puede fijar en "carpetas_claude")
    raices = CFG.get("carpetas_claude") or [os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")]
    for raiz in raices:
        for f in glob.glob(os.path.join(raiz, "projects", "**", "*.jsonl"), recursive=True):
            try:
                if os.path.getmtime(f) < desde:
                    continue
                for linea in open(f, encoding="utf-8", errors="replace"):
                    if '"usage"' not in linea:
                        continue
                    try:
                        d = json.loads(linea)
                    except ValueError:
                        continue
                    ts = d.get("timestamp")
                    if ts and dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() < desde:
                        continue
                    u = (d.get("message") or {}).get("usage") or {}
                    total += sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens"))
            except OSError:
                continue
    return total


def cuota_baja():
    """True si al dueno le queda menos del umbral (10 % por defecto) de su cuota semanal estimada; entonces pausa el agente."""
    try:
        c = rpc("t_config", **cred())
    except Exception:
        return False
    presu, umbral = c.get("presupuesto_semanal_tokens"), c.get("umbral_pausa_pct", 10)
    if not presu:
        return False
    usado = uso_semanal_local()
    queda = max(0.0, 1 - usado / presu) * 100
    if queda < umbral:
        rpc("t_pausar", **cred(), p_motivo="cuota_semanal_baja", p_detalle={"queda_pct": round(queda, 1), "umbral_pct": umbral})
        anotar(f"me pauso: queda ~{queda:.0f} % de la cuota semanal (umbral {umbral} %)")
        return True
    return False


# ======================= MODO NUBE (rutina de Claude Code en la nube, PC apagado) =======================
# En la nube la propia sesion de Claude de la rutina es el agente: este script solo le da comandos.
#   python trabajador.py --nube tomar        -> toma una tarea, baja el codigo en vivo, deja TAREA.md con todo el contexto
#   python trabajador.py --nube delegar      -> (misiones) publica SUBTAREAS.json como subtareas/opiniones
#   python trabajador.py --nube esperar 20   -> (misiones) espera hasta N min los resultados de lo delegado
#   python trabajador.py --nube sincronizar  -> sube/baja cambios de codigo (correr seguido mientras trabajas)
#   python trabajador.py --nube entregar     -> ultima sincronizacion, mensajes, notas y RESULTADO.md -> tarea hecha
#   python trabajador.py --nube devolver "motivo"
# Credenciales por variables de entorno secretas: RED_AGENTE_ID y RED_LLAVE.
ESTADO_NUBE = os.path.join(AQUI, "trabajo_nube", "estado.json")


def _estado(nuevo=None):
    if nuevo is not None:
        os.makedirs(os.path.dirname(ESTADO_NUBE), exist_ok=True)
        json.dump(nuevo, open(ESTADO_NUBE, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        return nuevo
    return json.load(open(ESTADO_NUBE, encoding="utf-8")) if os.path.exists(ESTADO_NUBE) else None


def _sinc_desde_estado(e):
    sz = Sincronizador(e["carpeta"], e["tarea"]["proyecto_id"], e["tarea"]["id"])
    sz.ver, sz.sync, sz.desde = e.get("ver", {}), e.get("sync", {}), e.get("desde")
    return sz


def _guardar_sinc(e, sz):
    e.update(ver=sz.ver, sync=sz.sync, desde=sz.desde); _estado(e)


def nube(accion, extra):
    e = _estado()
    if accion == "vigilar":   # espera barata (un solo comando): sin tarea -> toma la proxima; con tarea -> avisa mensajes/cambios nuevos
        fin = time.time() + 60 * min(float(extra or 10), 30)
        while time.time() < fin:
            e = _estado()
            if not e:
                import io, contextlib
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    nube("tomar", None)
                out = buf.getvalue()
                if out.startswith("TAREA"):
                    print(out); return
            else:
                t = e["tarea"]
                desde = e.get("leido_hasta") or (dt.datetime.utcnow() - dt.timedelta(minutes=30)).isoformat() + "Z"
                ms = rpc("t_mensajes", **cred(), p_proyecto=t["proyecto_id"], p_desde=desde, p_max=20) or []
                sz = _sinc_desde_estado(e); sz.ciclo(); _guardar_sinc(e, sz)
                if ms or sz.bajados:
                    if ms:
                        e["leido_hasta"] = ms[0]["ts"]; _estado(e)
                    for m in reversed(ms):
                        print(f"[{m['ts'][11:16]}] {m['de']}: {m['texto']}")
                    print(f"NOVEDADES: {len(ms)} mensaje(s), {sz.bajados} archivo(s) cambiados por otros"); return
            time.sleep(30)
        print("SIN_NOVEDADES"); return
    if accion == "tomar":
        if e:
            print(f"YA_TENGO_TAREA {e['tarea']['id']} en {e['carpeta']} (entrega o devuelve primero)"); return
        if not rpc("t_latido", **cred(), p_estado="libre"):
            print("EN_PAUSA"); return
        t = rpc("t_tomar", **cred(), p_minutos=CFG["minutos_por_tarea"])
        if not t or not t.get("id"):
            print("SIN_TAREAS"); return
        base = os.path.join(AQUI, "trabajo_nube", t["id"]); shutil.rmtree(base, ignore_errors=True); os.makedirs(base)
        repo = (t.get("config") or {}).get("repo_url")
        if repo:
            subprocess.run(["git", "clone", "--depth", "50", repo, base], capture_output=True, text=True)
        sz = Sincronizador(base, t["proyecto_id"], t["id"])
        try:
            sz.iniciar()
        except Exception as ex:
            anotar("codigo en vivo no disponible:", ex)
        mensajes = rpc("t_mensajes", **cred(), p_proyecto=t["proyecto_id"]) or []
        notas = rpc("t_notas", **cred(), p_proyecto=t["proyecto_id"]) or []
        os.makedirs(os.path.join(base, "NOTAS"), exist_ok=True)
        for n in notas:
            if n["ruta"].startswith("notas/"):
                open(os.path.join(base, "NOTAS", os.path.basename(n["ruta"])), "w", encoding="utf-8").write(n["contenido"])
        comp = (rpc("t_companeros", **cred(), p_proyecto=t["proyecto_id"]) or []) if t.get("modo") == "mision" else []
        texto = armar_prompt(t, mensajes, [n for n in notas if n["ruta"].startswith("notas/")]) + ajuste_de_nota(t) + prompt_modo(t, comp)
        texto += """
# Modo nube (tu eres el agente)
- Trabaja dentro de esta carpeta. Cada pocos minutos, y siempre despues de guardar archivos, corre:
  `python {s} --nube sincronizar`  (comparte tu codigo en vivo y trae el de los demas).
- Mision: escribe SUBTAREAS.json, corre `python {s} --nube delegar` y luego `python {s} --nube esperar 20`
  (puedes repetir esperar). Integra los resultados que imprime.
- Al terminar: RESULTADO.md (+ MENSAJES.txt y NOTAS/ si aplica) y `python {s} --nube entregar`.
- CONVERSACION EN VIVO: `python {s} --nube leer` trae los mensajes nuevos (personas y otros agentes);
  `python {s} --nube decir "texto"` (o `"<id_agente>: texto"`) responde al tiro. Lee al empezar, cada vez que sincronices
  y antes de entregar; si te preguntan algo, contesta.
- Si no puedes seguir: `python {s} --nube devolver "motivo"`.
""".format(s=os.path.abspath(__file__))
        open(os.path.join(base, "TAREA.md"), "w", encoding="utf-8").write(texto)
        nuevo = dict(tarea=t, carpeta=base, notas={x["ruta"]: x["contenido"] for x in notas}, delegadas=[])
        _guardar_sinc(nuevo, sz)
        print(f"TAREA {t['id']} [{t.get('modo', 'trabajo')}] «{t['titulo']}»\nCARPETA {base}\nLee {os.path.join(base, 'TAREA.md')} y trabaja.")
        return
    if not e:
        print("SIN_TAREA_ACTIVA (corre primero: --nube tomar)"); return
    t = e["tarea"]
    if accion == "sincronizar":
        sz = _sinc_desde_estado(e); sz.ciclo(); _guardar_sinc(e, sz); rpc("t_latido", **cred(), p_estado="trabajando")
        print(f"sincronizado: {sz.subidos} subidas, {sz.bajados} bajadas, conflictos: {sorted(sz.conflictos) or 'ninguno'}")
    elif accion == "delegar":
        ids = []
        for sbt in leer_subtareas(e["carpeta"]):
            ids.append(rpc("t_subtarea", **cred(), p_padre=t["id"], p_titulo=sbt["titulo"], p_descripcion=sbt["descripcion"],
                           p_modo=sbt.get("modo") or "trabajo", p_para=sbt.get("para") or None))
        e["delegadas"] += ids; _estado(e); print(f"delegadas {len(ids)} subtarea(s): {ids}")
    elif accion == "esperar":
        fin = time.time() + 60 * min(float(extra or 20), 30)
        while True:
            rpc("t_latido", **cred(), p_estado="esperando")
            est = rpc("t_mision", **cred(), p_padre=t["id"]) or []
            pend = [x for x in est if x["id"] in e["delegadas"] and x["estado"] not in ("hecha", "fallida", "cancelada")]
            if not pend or time.time() > fin:
                break
            time.sleep(30)
        for x in est:
            print(f"## {x['titulo']} [{x['modo']}] -> {x['estado']} ({x['agente'] or 'sin tomar'})\n{(x['resultado'] or '')[:6000]}\n")
        print(f"PENDIENTES {len(pend)}")
    elif accion == "entregar":
        sz = _sinc_desde_estado(e); sz.ciclo()
        n = publicar_mensajes(e["carpeta"], t["proyecto_id"])
        subir_conocimiento(e["carpeta"], t["proyecto_id"], e.get("notas", {}))
        f = os.path.join(e["carpeta"], "RESULTADO.md")
        resultado = open(f, encoding="utf-8", errors="replace").read() if os.path.exists(f) else "(sin RESULTADO.md)"
        rpc("t_terminar", **cred(), p_tarea=t["id"], p_ok=os.path.exists(f), p_resultado=resultado)
        os.remove(ESTADO_NUBE); print(f"ENTREGADA ({n} mensajes, {sz.subidos} archivos subidos)")
    elif accion == "leer":                                   # mensajes nuevos del proyecto (y su mesa) desde la ultima lectura
        desde = e.get("leido_hasta") or (dt.datetime.utcnow() - dt.timedelta(hours=2)).isoformat() + "Z"
        ms = rpc("t_mensajes", **cred(), p_proyecto=t["proyecto_id"], p_desde=desde, p_max=50) or []
        for m in reversed(ms):
            print(f"[{m['ts'][11:16]}] {m['de']}{' -> ' + str(m['para']) if m.get('para') else ''}: {m['texto']}")
        if ms:
            e["leido_hasta"] = ms[0]["ts"]; _estado(e)
        print(f"{len(ms)} mensaje(s) nuevo(s)")
    elif accion == "decir":                                  # --nube decir "texto"  |  --nube decir "<id_agente>: texto"
        para, texto = None, extra or ""
        if re.match(r"^[0-9a-f-]{36}:", texto):
            para, texto = texto.split(":", 1)
        rpc("t_mensaje", **cred(), p_proyecto=t["proyecto_id"], p_texto=texto.strip(), p_para=para); print("enviado")
    elif accion == "devolver":
        rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo=(extra or "devuelta")[:60]); os.remove(ESTADO_NUBE); print("DEVUELTA")
    else:
        print("accion desconocida")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--config", default=os.path.join(AQUI, "trabajador.json"))
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--nube", nargs="+", metavar=("ACCION", "ARG"), help="modo rutina en la nube: tomar|vigilar|sincronizar|leer|decir|delegar|esperar|entregar|devolver")
    a = ap.parse_args()
    if os.path.exists(a.config):
        CFG.update(json.load(open(a.config, encoding="utf-8")))
    CFG["agente_id"] = os.environ.get("RED_AGENTE_ID") or CFG["agente_id"]
    CFG["llave"] = os.environ.get("RED_LLAVE") or CFG["llave"]
    if a.nube:
        if not CFG["agente_id"]:
            sys.exit("falta RED_AGENTE_ID en las variables del entorno")
        # sin RED_LLAVE: la llave la agrega el "network secret" del entorno en la cabecera x-red-llave (recomendado)
        return nube(a.nube[0], a.nube[1] if len(a.nube) > 1 else None)
    for k in ("clave_publica", "agente_id", "llave"):
        if not CFG[k]:
            sys.exit(f"falta '{k}' en {a.config}")
    if not shutil.which("claude"):
        sys.exit("no encuentro `claude` (Claude Code) en el PATH")
    hechas, dia = 0, dt.date.today()
    while True:
        if dt.date.today() != dia:
            hechas, dia = 0, dt.date.today()
        try:
            activo = rpc("t_latido", **cred(), p_estado="libre")
        except Exception as e:
            anotar("sin conexion:", e); time.sleep(60); continue
        if not activo or not en_horario() or hechas >= CFG["max_tareas_dia"]:
            if a.una_vez: return
            anotar("en pausa / fuera de horario / cupo del dia usado"); time.sleep(CFG["espera_sin_tareas_s"]); continue
        responder_bandeja()
        if cuota_baja():
            if a.una_vez: return
            time.sleep(CFG["espera_sin_tareas_s"]); continue
        t = rpc("t_tomar", **cred(), p_minutos=CFG["minutos_por_tarea"])
        if not t or not t.get("id"):
            if a.una_vez: return
            dormir_atento(CFG["espera_sin_tareas_s"]); continue
        r = hacer(t)
        hechas += r == "ok"
        if a.una_vez: return
        if r == "limite":
            time.sleep(CFG["espera_limite_s"])
        elif r == "pausa":
            time.sleep(CFG["espera_sin_tareas_s"])


if __name__ == "__main__":
    main()
