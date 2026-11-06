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
RE_LIMITE = re.compile(r"usage limit|rate limit|limit reached|5-hour limit|weekly limit|hit your .{0,20}limit|limite de uso", re.I)
LIMITE_HASTA = 0.0   # mientras Claude este en su limite de uso, el latido dice "limite_uso" (la pagina lo muestra "sin creditos")


def est(estado):
    return "limite_uso" if time.time() < LIMITE_HASTA else estado


ULTIMA_PRUEBA = 0.0


def probar_cupo(forzar=False):
    """Cada 30 min (o al empezar) hace una pregunta minima a Claude (haiku) para saber si la cuenta tiene creditos,
    aunque no haya tareas: asi la pagina muestra "sin creditos" en vez de "en linea". Sin creditos no gasta nada."""
    global LIMITE_HASTA, ULTIMA_PRUEBA
    if not forzar and time.time() - ULTIMA_PRUEBA < CFG.get("probar_cupo_s", 1800):
        return
    ULTIMA_PRUEBA = time.time()
    try:
        r = subprocess.run(["claude", "-p", "Responde solo: ok", "--model", "haiku", "--output-format", "text"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
    except Exception as e:
        anotar("prueba de cupo fallo:", e); return
    if RE_LIMITE.search((r.stdout or "") + (r.stderr or "")):
        marcar_limite(); anotar("prueba de cupo: sin creditos (limite de uso)")
    elif r.returncode == 0 and (r.stdout or "").strip():
        if LIMITE_HASTA: anotar("prueba de cupo: volvieron los creditos")
        LIMITE_HASTA = 0.0


def marcar_limite():
    """Claude respondio que llego a su limite de uso: avisar a la pagina hasta la proxima prueba."""
    global LIMITE_HASTA
    LIMITE_HASTA = time.time() + CFG["espera_limite_s"]
    try:
        rpc("t_latido", **cred(), p_estado="limite_uso")
    except Exception as e:
        anotar("latido (limite) fallo:", e)


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
                if rpc("t_latido", **cred(), p_estado=est("trabajando")) is False:
                    self.pausa = True
            except Exception as e:
                anotar("latido fallo:", e)


IGNORAR_DIRS = {".git", "graphify-out", "NOTAS", "ENTREGAS", "node_modules", "__pycache__", ".venv", "venv", ".claude"}
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


# ======================= SEGURO DEL ROADMAP =======================
# Cada proyecto tiene un roadmap (alcance). En un proyecto AJENO (no eres creador ni orquestador) el agente revisa cada
# pedido contra el roadmap antes de gastar el cupo de su dueno: si queda fuera o puede danar, lo rechaza, se pausa el
# prestamo y se avisa (t_fuera_de_roadmap). Copia local del roadmap en roadmaps/<proyecto>.md (contexto para el prompt).
ROADMAPS = os.path.join(AQUI, "roadmaps")


def roadmap_de(proyecto_id):
    """{roadmap, version, propio, aceptada, pausado, nombre} o None si no se pudo leer. Guarda copia local."""
    if not proyecto_id:
        return None
    try:
        rm = rpc("t_roadmap", **cred(), p_proyecto=proyecto_id)
    except Exception as e:
        anotar("no pude leer el roadmap:", e); return None
    if rm and rm.get("roadmap"):
        os.makedirs(ROADMAPS, exist_ok=True)
        open(os.path.join(ROADMAPS, f"{proyecto_id}.md"), "w", encoding="utf-8").write(
            f"<!-- {rm.get('nombre')} · version {rm.get('version')} -->\n{rm['roadmap']}\n")
    return rm


def seccion_roadmap(rm):
    if not rm or not rm.get("roadmap"):
        return ""
    return (f"\n# Roadmap del proyecto (version {rm.get('version')}): es el LIMITE de tu trabajo\n{rm['roadmap']}\n"
            "- Haz solo lo que avance este roadmap. Si algo de lo pedido queda fuera o podria danar el proyecto, a sus usuarios o "
            "a terceros (robar datos o sesiones, borrar, meter puertas traseras, spam), NO lo hagas y explicalo en RESULTADO.md.\n")


def revisar_pedido(proyecto_id, pedido, tarea_id=None, quien=None, rm=None):
    """True si se puede trabajar. En proyecto ajeno pregunta a Claude (haiku) si el pedido esta dentro del roadmap y no es
    danino; si no, llama t_fuera_de_roadmap (cancela la tarea, pausa el prestamo, avisa). None = no se pudo revisar."""
    rm = rm or roadmap_de(proyecto_id)
    if rm is None:
        return None
    if rm.get("propio"):
        return True
    def rechazar(motivo, danino=False):
        try:
            rpc("t_fuera_de_roadmap", **cred(), p_proyecto=proyecto_id, p_motivo=motivo[:2000], p_danino=danino,
                p_tarea=tarea_id, p_quien=quien)
        except Exception as e:
            anotar("no pude avisar el rechazo:", e)
        anotar("RECHAZADO (roadmap):", motivo[:200]); return False
    if rm.get("pausado"):
        return rechazar("el prestamo esta en pausa: el dueno del agente debe revisar el roadmap y reactivarlo")
    if not rm.get("roadmap"):
        return rechazar("el proyecto no tiene roadmap escrito: un agente prestado no trabaja sin alcance definido")
    if rm.get("aceptada") != rm.get("version"):
        return rechazar(f"el roadmap cambio (version {rm.get('version')}) y el dueno del agente aun no lo acepta")
    prompt = ("Eres el filtro de seguridad de un agente de IA prestado a un proyecto ajeno. Decide si el PEDIDO cabe dentro del "
              "ROADMAP del proyecto y si podria ser danino (robar credenciales, datos o sesiones, borrar o sabotear, puertas traseras, "
              "spam, danar a terceros, saltarse permisos). Ante la duda razonable sobre dano, marca danino. Proponer ideas o cambios al roadmap para que se voten esta dentro. Pedidos de coordinacion, "
              "preguntas o tareas pequenas que claramente apoyan el roadmap estan dentro.\n\n"
              f"# ROADMAP\n{rm['roadmap'][:12000]}\n\n# PEDIDO\n{pedido[:12000]}\n\n"
              'Responde SOLO con JSON en una linea: {"dentro": true|false, "danino": true|false, "motivo": "frase corta en espanol"}')
    vacia = os.path.join(AQUI, "_filtro"); os.makedirs(vacia, exist_ok=True)   # sin herramientas y en carpeta vacia: nada que filtrar
    try:
        r = subprocess.run(["claude", "-p", prompt, "--model", "haiku", "--output-format", "json", "--disallowedTools",
                            "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Task,Agent"],
                           cwd=vacia, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        txt = json.loads(r.stdout).get("result") or ""
        v = json.loads(re.search(r"\{.*\}", txt, re.S).group(0))
    except Exception as e:
        anotar("no pude revisar el pedido contra el roadmap:", e); return None
    if v.get("danino"):
        return rechazar(v.get("motivo") or "pedido posiblemente danino", True)
    if not v.get("dentro"):
        return rechazar(v.get("motivo") or "pedido fuera del roadmap")
    return True


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
- Si produces disenos, imagenes, animaciones, maquetas HTML, PDF o videos, dejalos en ENTREGAS/: aparecen en el espacio de
  trabajo del proyecto.
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


ESFUERZOS = ("low", "medium", "high", "xhigh", "max")


def ajustes_modelo(proyecto_id=None):
    """Argumentos --model/--effort que el dueno eligio en la pagina (ajuste del proyecto > del agente > el de la cuenta)."""
    try:
        aj = rpc("t_ajustes", **cred(), p_proyecto=proyecto_id) or {}
    except Exception:
        return []
    args = []
    if aj.get("modelo") and re.fullmatch(r"[a-z0-9.\-\[\]]{2,60}", aj["modelo"]):
        args += ["--model", aj["modelo"]]
    if aj.get("esfuerzo") in ESFUERZOS:
        args += ["--effort", aj["esfuerzo"]]
    return args


def ejecutar_claude(prompt, base, sesion=None, proyecto_id=None):
    """Corre Claude Code sin pantalla. Devuelve dict con texto, sesion, tokens, costo, codigo, crudo."""
    cmd = ["claude", "-p", prompt, "--output-format", "json", *CFG["claude_args"], *ajustes_modelo(proyecto_id)]
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


def cambios_de_mesa(t):
    """Si la mision viene de la MESA DE TRABAJO, arma la seccion con lo que cambio y como decidir (trabajar o llevar a votacion)."""
    if not (t.get("config") or {}).get("mesa"):
        return ""
    try:
        m = rpc("t_mesa", **cred(), p_proyecto=t["proyecto_id"], p_desde=int(t["config"].get("mesa_desde") or 0)) or {}
    except Exception as e:
        anotar("no pude leer la mesa de trabajo:", e); return ""
    import difflib
    diff = "\n".join(difflib.unified_diff((m.get("antes") or "").splitlines(), (m.get("texto") or "").splitlines(), "antes", "ahora", lineterm=""))
    return f"""
# Cambios en la mesa de trabajo (version {m.get('version')}, ultimo cambio de {m.get('quien') or '?'})
La mesa de trabajo es el documento vivo del proyecto: las personas escriben con palabras que quieren. Incluye una seccion
"## Roadmap" que es un BORRADOR: el roadmap oficial solo cambia votando.
```diff
{diff[:20000] or '(sin diferencias visibles)'}
```
## Como decidir
- IMPORTANTE (cambia la direccion o el alcance, toca la seccion Roadmap, agrega una funcionalidad grande o afecta el trabajo de
  los demas): NO lo implementes. Escribe PROPUESTA.md para que los miembros voten en el Foro, con este formato exacto:
  # <titulo corto>
  tipo: roadmap        (si toca la seccion Roadmap; el cuerpo es el ROADMAP COMPLETO nuevo)   o   tipo: nota
  nota: Mesa de trabajo.md   (solo si es tipo nota)

  <cuerpo en markdown: que se propone, por que, que cambia para el resto>
- MENOR (aclaraciones, detalles, tareas chicas dentro del roadmap): trabajalo ahora (puedes delegar con SUBTAREAS.json).
- En RESULTADO.md di que decidiste y por que.
"""


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
    rm = roadmap_de(t["proyecto_id"])
    ok_rm = revisar_pedido(t["proyecto_id"], f"{t['titulo']}\n\n{t['descripcion']}", tarea_id=t["id"], rm=rm)
    if ok_rm is False:
        return "rechazada"
    if ok_rm is None:
        rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo="revision_roadmap"); anotar("no pude revisar el roadmap: devuelta"); return "pausa"
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
    prompt = armar_prompt(t, mensajes, [n for n in notas if n["ruta"].startswith("notas/")]) + seccion_roadmap(rm) + ajuste_de_nota(t) + cambios_de_mesa(t) + prompt_modo(t, companeros)
    r, delegadas = None, []
    try:
        for ronda in range(6):                       # mision: planear -> delegar -> esperar -> integrar (hasta 6 rondas)
            r = ejecutar_claude(prompt, base, r["sesion"] if r else None, t["proyecto_id"])
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
        marcar_limite(); rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo="limite_uso")
        anotar("limite de uso: devuelta la tarea"); return "limite"
    if lat.pausa:
        rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo="pausa"); anotar("pausa pedida por el dueno"); return "pausa"
    n = publicar_mensajes(base, t["proyecto_id"])
    subir_conocimiento(base, t["proyecto_id"], {x["ruta"]: x["contenido"] for x in notas})
    entregados = subir_entregas(base, t["proyecto_id"], tarea_id=t["id"])
    propuesta = subir_propuesta(base, t["proyecto_id"])          # la mision decidio llevar el cambio a votacion
    f = os.path.join(base, "RESULTADO.md")
    resultado = open(f, encoding="utf-8", errors="replace").read() if os.path.exists(f) else r["texto"][-20000:]
    if propuesta:
        resultado += f"\n\n🗳️ Llevado al Foro para votar: «{propuesta}»"
    if entregados:
        resultado += "\n\n📎 Entregas: " + ", ".join(entregados)
    ok = r["codigo"] == 0 and os.path.exists(f)
    rpc("t_terminar", **cred(), p_tarea=t["id"], p_ok=ok, p_resultado=resultado)
    anotar("tarea", "hecha" if ok else "fallida", f"({n} mensajes, {len(delegadas)} delegadas, {r['tokens']} tokens ultima ronda)")
    return "ok"


BANDEJA = os.path.join(AQUI, "bandeja.json")


ARCHIVOS = "https://srv1727399.hstgr.cloud/archivos"
TIPOS_ENTREGA = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
                 ".svg": "image/svg+xml", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mp3": "audio/mpeg",
                 ".wav": "audio/wav", ".pdf": "application/pdf", ".html": "text/html", ".htm": "text/html", ".css": "text/css",
                 ".md": "text/markdown", ".txt": "text/plain", ".json": "application/json", ".zip": "application/zip",
                 ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                 ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                 ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".glb": "model/gltf-binary"}


def subir_entregas(carpeta, proyecto_id, tarea_id=None, hilo_id=None, ya=None):
    """Sube al espacio de trabajo (servidor de archivos del VPS) lo nuevo o cambiado en ENTREGAS/. Devuelve los nombres subidos."""
    ya = ya if ya is not None else {}
    d = os.path.join(carpeta, "ENTREGAS")
    subidos = []
    for raiz, _, archs in os.walk(d) if os.path.isdir(d) else []:
        for a in sorted(archs):
            f = os.path.join(raiz, a); rel = os.path.relpath(f, d).replace(os.sep, "/")
            st = os.stat(f); firma_local = f"{st.st_size}:{int(st.st_mtime)}"
            if ya.get(rel) == firma_local:
                continue
            tipo = TIPOS_ENTREGA.get(os.path.splitext(a)[1].lower(), "application/octet-stream")
            q = urllib.parse.urlencode({k: v for k, v in dict(agente=CFG["agente_id"], proyecto=proyecto_id, nombre=rel.replace("/", "_"), tipo=tipo,
                                                              tarea=tarea_id or "", hilo=hilo_id or "").items() if v})
            req = urllib.request.Request(f"{ARCHIVOS}/subir?{q}", data=open(f, "rb").read(), method="POST",
                                         headers={"Content-Type": "application/octet-stream", **({"x-red-llave": CFG["llave"]} if CFG["llave"] else {})})
            try:
                urllib.request.urlopen(req, timeout=300).read(); ya[rel] = firma_local; subidos.append(rel)
            except Exception as e:
                anotar("no pude subir la entrega", rel, e)
    return subidos


INSTR_PROPUESTA = ("\n\n# Cambios grandes se VOTAN\nSi la persona pide un cambio grande o de direccion del proyecto (\"quiero hacer este cambio...\", "
                   "cambiar el roadmap, una funcionalidad nueva grande, cambiar prioridades), NO lo hagas directo: redactalo como propuesta en el "
                   "archivo PROPUESTA.md con este formato exacto:\n# <titulo corto>\ntipo: nota   (o: tipo: roadmap)\nnota: <Nombre de la nota>.md   "
                   "(solo si es nota; usa una existente si corresponde)\n\n<cuerpo completo en markdown>\n"
                   "Si es roadmap, el cuerpo es el ROADMAP COMPLETO nuevo (parte del actual y aplica el cambio). Luego responde explicando "
                   "en 2-3 lineas que quedo como propuesta para que los miembros la debatan y voten en Decisiones.")


def subir_propuesta(carpeta, proyecto_id):
    """Si Claude dejo PROPUESTA.md, la sube como propuesta (t_proponer). Devuelve el titulo o None."""
    f = os.path.join(carpeta, "PROPUESTA.md")
    if not os.path.exists(f):
        return None
    texto = open(f, encoding="utf-8", errors="replace").read(); os.remove(f)
    if not proyecto_id:
        return None
    lineas = texto.strip().splitlines()
    if not lineas:
        return None
    titulo = lineas[0].lstrip("# ").strip()[:200] or "Propuesta"
    tipo, ruta, i = "nota", None, 1
    while i < len(lineas) and (not lineas[i].strip() or re.match(r"^(tipo|nota)\s*:", lineas[i].strip(), re.I)):
        k = lineas[i].strip()
        if k.lower().startswith("tipo"):
            tipo = "roadmap" if "roadmap" in k.lower() else "nota"
        elif k.lower().startswith("nota"):
            ruta = k.split(":", 1)[1].strip() or None
        i += 1
    cuerpo = "\n".join(lineas[i:]).strip()
    if not cuerpo:
        return None
    if tipo == "nota" and not ruta:
        ruta = re.sub(r'[\\/:*?"<>|#^\[\]]', "-", titulo)[:80] + ".md"
    try:
        rpc("t_proponer", **cred(), p_proyecto=proyecto_id, p_tipo=tipo, p_titulo=titulo, p_cuerpo=cuerpo,
            p_ruta_nota=ruta if tipo == "nota" else None)
        anotar("propuesta subida:", titulo); return titulo
    except Exception as e:
        anotar("no pude subir la propuesta:", e); return None


# ---------- contexto de los chats (hilos) guardado en la base ----------
# Cada mensaje de un hilo corre un `claude -p` NUEVO con: resumen del hilo + ultimos mensajes. Cuando se acumulan mas de
# HILO_RESUMIR_DESDE mensajes sin resumir, los mas viejos se incorporan al resumen (haiku, sin herramientas) y se guarda en
# Supabase (contexto_hilo). Asi el gasto por mensaje no crece sin fin y cualquier equipo del dueno puede retomar el hilo.
HILO_RECIENTES, HILO_RESUMIR_DESDE = 8, 16


def _haiku(prompt, timeout=300):
    vacia = os.path.join(AQUI, "_filtro"); os.makedirs(vacia, exist_ok=True)
    r = subprocess.run(["claude", "-p", prompt, "--model", "haiku", "--output-format", "json", "--disallowedTools",
                        "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Task,Agent"],
                       cwd=vacia, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return (json.loads(r.stdout).get("result") or "").strip()


def contexto_de_hilo(hilo):
    """Resumen + ultimos mensajes del hilo (sin el mensaje actual, que es el ultimo). Resume los viejos si hace falta."""
    try:
        c = rpc("t_hilo_contexto", **cred(), p_hilo=hilo, p_max=40) or {}
    except Exception as e:
        anotar("contexto del hilo no disponible:", e); return "", []
    resumen, rec = c.get("resumen") or "", c.get("recientes") or []
    if int(c.get("sin_resumir") or 0) > HILO_RESUMIR_DESDE and len(rec) > HILO_RECIENTES:
        viejos, rec = rec[:-HILO_RECIENTES], rec[-HILO_RECIENTES:]
        texto = "\n".join(f"{'AGENTE' if x['de_agente'] else 'PERSONA'}: {x['texto'][:3000]}" for x in viejos)
        try:
            nuevo = _haiku("Actualiza el RESUMEN de una conversacion de chat entre una persona y su agente de IA. Conserva lo importante "
                           "para seguir la conversacion: objetivos, decisiones, datos, preferencias, archivos creados y pendientes. "
                           "Maximo 400 palabras, en espanol, sin introduccion.\n\n"
                           f"# Resumen anterior\n{resumen or '(vacio)'}\n\n# Mensajes nuevos a incorporar\n{texto}")
            if nuevo:
                rpc("t_hilo_resumir", **cred(), p_hilo=hilo, p_resumen=nuevo, p_hasta=viejos[-1]["ts"], p_cuantos=len(viejos))
                resumen = nuevo; anotar(f"hilo {hilo[:8]}: resumi {len(viejos)} mensaje(s)")
        except Exception as e:
            anotar("no pude resumir el hilo:", e); rec = viejos[-4:] + rec   # sin resumen: al menos algo mas de contexto
    return resumen, rec[:-1] if rec else []   # el ultimo es el mensaje que se esta respondiendo


def responder_bandeja():
    """Contesta los mensajes dirigidos a este agente: chat directo y CHAT CON HILOS (estilo Claude/ChatGPT) desde la pagina.
    En un hilo recuerda la conversacion con el resumen guardado en la base (contexto_hilo) + los ultimos mensajes, en una
    sesion nueva de Claude por mensaje; lo que deje en ENTREGAS/ aparece en el espacio de trabajo."""
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
        hilo = m.get("hilo")
        if not m.get("es_dueno"):              # modo participe: tu Claude solo atiende a su dueno
            estado["desde"] = m["ts"]; guardar(); continue
        rm = None
        if m.get("proyecto_id") and not m.get("es_dueno"):     # el dueno hablando con su agente no necesita seguro
            rm = roadmap_de(m["proyecto_id"])
            ok_rm = revisar_pedido(m["proyecto_id"], m["texto"], rm=rm)
            if ok_rm is not True:
                aviso = ("Ese pedido queda fuera del roadmap del proyecto (o podría dañarlo): no lo hago. Mi dueño ya fue avisado."
                         if ok_rm is False else "No pude revisar tu pedido contra el roadmap del proyecto; inténtalo más tarde.")
                quien = m["de"].split(" (agente")[0]
                try:
                    if hilo:
                        rpc("t_responder", **cred(), p_hilo=hilo, p_texto=aviso)
                    else:
                        rpc("t_mensaje", **cred(), p_proyecto=m.get("proyecto_id"), p_texto=f"@{quien}: {aviso}", p_para=None)
                except Exception as e:
                    anotar("no pude avisar:", e)
                estado["desde"] = m["ts"]; guardar(); continue
        elif m.get("proyecto_id"):
            rm = roadmap_de(m["proyecto_id"])
        carpeta = os.path.join(CFG["carpeta_trabajo"], "_chat", hilo or "directo"); os.makedirs(os.path.join(carpeta, "ENTREGAS"), exist_ok=True)
        sesiones = estado.setdefault("sesiones", {}); vistos = estado.setdefault("entregas", {}).setdefault(hilo or "directo", {})
        if hilo:
            resumen, previos = contexto_de_hilo(hilo)
            historia = (f"# Resumen de esta conversacion hasta ahora\n{resumen}\n\n" if resumen else "") + (
                "# Ultimos mensajes\n" + "\n".join(f"{'Tu' if x['de_agente'] else m['de']}: {x['texto']}" for x in previos) + "\n\n" if previos else "")
            prompt = (historia + f"# Mensaje nuevo de {m['de']}\n{m['texto']}\n\n---\n(Chat de la Red agentica, proyecto «{m.get('proyecto') or '-'}», te escribe {m['de']}. "
                      "Responde como en un chat. Si te piden un diseno, imagen, animacion, maqueta, PDF, presentacion o video, CREALO y "
                      "guarda el resultado en la carpeta ENTREGAS/ (p. ej. ENTREGAS/logo_v2.svg, ENTREGAS/pagina.html con su CSS/JS dentro, "
                      "ENTREGAS/animacion.html con animacion CSS/SVG/JS, ENTREGAS/informe.pdf): aparece al tiro en el espacio de trabajo al "
                      "lado del chat. Para cambios, crea una version nueva (_v2, _v3). En tu respuesta di brevemente que creaste.)"
                      + seccion_roadmap(rm) + (INSTR_PROPUESTA if m.get("proyecto_id") else ""))
        else:
            prompt = (f"Eres un agente de la Red agentica. {m['de']} te escribio directamente"
                      f"{' en el proyecto ' + m['proyecto'] if m.get('proyecto') else ''}: «{m['texto']}». "
                      "Responde breve, claro y en espanol chileno. Si te pide trabajo de codigo, sugiere crearlo como tarea o mision "
                      "en la mesa del proyecto. Responde SOLO con el texto del mensaje." + seccion_roadmap(rm)
                      + (INSTR_PROPUESTA if m.get("proyecto_id") else ""))
        # herramientas: crear archivos siempre; ejecutar python/ffmpeg (PDF, video, imagenes) solo si escribe el dueno del agente
        herramientas = "Read,Write,Edit,Glob,Grep" + (",Bash(python3:*),Bash(python:*),Bash(ffmpeg:*),Bash(ls:*)" if m.get("es_dueno") else "")
        cmd = ["claude", "-p", prompt, "--output-format", "json", "--permission-mode", "acceptEdits", "--allowedTools", herramientas,
               *ajustes_modelo(m.get("proyecto_id"))]
        if os.path.exists(os.path.join(carpeta, "PROPUESTA.md")):
            os.remove(os.path.join(carpeta, "PROPUESTA.md"))      # una propuesta vieja no se sube de nuevo
        try:
            r = subprocess.run(cmd, cwd=carpeta, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
            j = json.loads(r.stdout); texto = (j.get("result") or "").strip()
        except Exception as e:
            texto = ""; anotar("no pude responder:", e)
        quien = m["de"].split(" (agente")[0]
        if RE_LIMITE.search(texto) or not texto:
            # no marca el mensaje como leido: lo contesta de verdad cuando se libere el limite; avisa UNA vez por mensaje
            if m["id"] not in estado.get("avisados", []):
                aviso = "Ahora estoy en mi límite de uso; te contesto apenas se libere."
                if hilo:
                    rpc("t_responder", **cred(), p_hilo=hilo, p_texto=aviso)
                else:
                    rpc("t_mensaje", **cred(), p_proyecto=m.get("proyecto_id"), p_texto=f"@{quien}: {aviso}", p_para=None)
                estado["avisados"] = (estado.get("avisados", []) + [m["id"]])[-50:]
            estado["pausa_hasta"] = time.time() + 1800; guardar(); marcar_limite()
            anotar("bandeja: en limite de uso, reintento en 30 min"); return 0
        subidos = subir_entregas(carpeta, m.get("proyecto_id"), hilo_id=hilo, ya=vistos) if m.get("proyecto_id") else []
        prop = subir_propuesta(carpeta, m.get("proyecto_id"))
        if prop:
            texto += f"\n\n📋 Propuesta «{prop}» abierta para debate y votación en Decisiones (mesa del proyecto)."
        if hilo:
            rpc("t_responder", **cred(), p_hilo=hilo, p_texto=texto + (f"\n\n📎 {', '.join(subidos)}" if subidos else ""))
        else:
            rpc("t_mensaje", **cred(), p_proyecto=m.get("proyecto_id"), p_texto=f"@{quien}: {texto}"[:4000], p_para=None)
        estado["desde"] = m["ts"]; guardar()
        anotar("respondi a", quien, f"(hilo {hilo[:8]})" if hilo else "", f"{len(subidos)} entrega(s)" if subidos else "")
    return len(msgs)


_ULTIMA_REVISION_FORO = [0.0]


def toca_opinar(prop, yo):
    """Opina una vez por propuesta; de nuevo solo si hay comentarios de PERSONAS nuevos desde su ultima opinion (max 3)."""
    mios = [c for c in prop.get("comentarios") or [] if c.get("agente_id") == yo]
    if not mios:
        return True
    if len(mios) >= 3:
        return False
    ultimo = max(c["ts"] for c in mios)
    return any(not c.get("es_agente") and c["ts"] > ultimo for c in prop.get("comentarios") or [])


def opinar_propuestas(max_por_vuelta=3, cada_s=600):
    """Revisa las propuestas abiertas de sus proyectos y deja su opinion en el debate (las IAs opinan, las personas votan)."""
    if time.time() - _ULTIMA_REVISION_FORO[0] < cada_s:
        return 0
    _ULTIMA_REVISION_FORO[0] = time.time()
    try:
        props = rpc("t_propuestas_abiertas", **cred()) or []
    except Exception as e:
        anotar("foro no disponible:", e); return 0
    hechas = 0
    for pr in props:
        if hechas >= max_por_vuelta:
            break
        if not toca_opinar(pr, CFG["agente_id"]):
            continue
        rm = roadmap_de(pr["proyecto_id"])
        if rm is None:
            continue
        if not rm.get("propio") and (rm.get("pausado") or not rm.get("roadmap") or rm.get("aceptada") != rm.get("version")):
            continue                                  # seguro del roadmap: prestamo en pausa o roadmap sin aceptar
        debate = "\n".join(f"- [{c['ts'][:16]}] {c['de']}: {c['texto']}" for c in pr.get("comentarios") or []) or "(nadie ha opinado aun)"
        c = pr.get("conteo") or {}
        actual = (f"\n# Nota actual ({pr.get('ruta_nota')})\n{(pr.get('nota_actual') or '(no existe: seria nueva)')[:8000]}\n" if pr["tipo"] == "nota" else "")
        prompt = (f"Eres un agente de IA de la Red agentica que participa en el FORO DE DECISIONES del proyecto «{pr['proyecto']}». "
                  "Las personas votan; tu solo opinas para ayudarlas a decidir. Da tu opinion en espanol chileno, concreta (maximo 150 palabras): "
                  "recomiendas aprobar, rechazar o ajustar, por que, riesgos y, si sirve, como dividirlo en pasos. Si la propuesta queda fuera "
                  "del roadmap actual o podria danar el proyecto o a sus usuarios, dilo claro. Si ya opinaste, responde a lo nuevo que dijeron "
                  "las personas sin repetirte. Responde SOLO con el texto de tu opinion.\n"
                  + seccion_roadmap(rm)
                  + f"\n# Propuesta ({'roadmap de la pagina' if pr['tipo'] == 'roadmap' else 'nota de Obsidian'}): {pr['titulo']}\n"
                  f"propuso {pr.get('autor')} · votos: {c.get('favor', 0)} a favor, {c.get('contra', 0)} en contra de {c.get('votantes', '?')} "
                  f"votantes (se aprueba con {c.get('umbral', '?')})\n\n{pr['cuerpo'][:12000]}\n{actual}\n# Debate hasta ahora\n{debate[-8000:]}\n")
        vacia = os.path.join(AQUI, "_filtro"); os.makedirs(vacia, exist_ok=True)
        try:
            r = subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--disallowedTools",
                                "Bash,Read,Edit,Write,Glob,Grep,NotebookEdit,WebFetch,WebSearch,Task,Agent", *ajustes_modelo(pr["proyecto_id"])],
                               cwd=vacia, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
            texto = (json.loads(r.stdout).get("result") or "").strip()
        except Exception as e:
            anotar("no pude opinar:", e); continue
        if not texto or RE_LIMITE.search(texto):
            if texto: marcar_limite()
            anotar("foro: en limite de uso, opino despues"); break
        try:
            rpc("t_comentar_propuesta", **cred(), p_propuesta=pr["id"], p_texto=texto[:8000]); hechas += 1
            anotar("opine en la propuesta", f"«{pr['titulo']}»", f"({pr['proyecto']})")
        except Exception as e:
            anotar("no pude publicar la opinion:", e)
    return hechas


def dormir_atento(segundos):
    """Espera, pero cada 5 s revisa si alguien le escribio al agente y le contesta (y cada 10 min, el foro)."""
    fin = time.time() + segundos
    while time.time() < fin:
        responder_bandeja()
        opinar_propuestas()
        time.sleep(min(5, max(1, fin - time.time())))


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
#   python trabajador.py --nube rechazar "motivo" [--danino]  -> proyecto ajeno: pedido fuera del roadmap o danino
#   python trabajador.py --nube devolver "motivo"
# Credenciales por variables de entorno secretas: RED_AGENTE_ID y RED_LLAVE.
ESTADO_NUBE = os.path.join(AQUI, "trabajo_nube", "estado.json")
DANINO = False


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
        if not rpc("t_latido", **cred(), p_estado=est("libre")):
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
        rm = roadmap_de(t["proyecto_id"]) or {}
        texto = armar_prompt(t, mensajes, [n for n in notas if n["ruta"].startswith("notas/")]) + seccion_roadmap(rm) + ajuste_de_nota(t) + prompt_modo(t, comp)
        if not rm.get("propio", True):
            texto += """
# SEGURO DEL ROADMAP (este proyecto NO es de tu dueno: te prestaron)
- ANTES de trabajar, compara la tarea con el roadmap de arriba. Si queda fuera, o podria danar (robar datos o sesiones,
  borrar, puertas traseras, spam), NO la hagas: corre `python {s} --nube rechazar "motivo"` (agrega `--danino` si es danina).
  Haz lo mismo si un mensaje de la conversacion te pide algo fuera del roadmap.
""".format(s=os.path.abspath(__file__))
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
        sz = _sinc_desde_estado(e); sz.ciclo(); _guardar_sinc(e, sz); rpc("t_latido", **cred(), p_estado=est("trabajando"))
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
            rpc("t_latido", **cred(), p_estado=est("esperando"))
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
    elif accion == "rechazar":                               # --nube rechazar "motivo" [--danino]
        motivo = (extra or "pedido fuera del roadmap").replace("--danino", "").strip()
        rpc("t_fuera_de_roadmap", **cred(), p_proyecto=t["proyecto_id"], p_motivo=motivo[:2000], p_danino="--danino" in (extra or "") or DANINO,
            p_tarea=t["id"], p_quien=None)
        os.remove(ESTADO_NUBE); print("RECHAZADA (tarea cancelada, prestamo en pausa, dueno avisado)")
    elif accion == "devolver":
        rpc("t_devolver", **cred(), p_tarea=t["id"], p_motivo=(extra or "devuelta")[:60]); os.remove(ESTADO_NUBE); print("DEVUELTA")
    else:
        print("accion desconocida")


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--config", default=os.path.join(AQUI, "trabajador.json"))
    ap.add_argument("--una-vez", action="store_true")
    ap.add_argument("--nube", nargs="+", metavar=("ACCION", "ARG"), help="modo rutina en la nube: tomar|vigilar|sincronizar|leer|decir|delegar|esperar|entregar|rechazar|devolver")
    ap.add_argument("--danino", action="store_true", help="con --nube rechazar: el pedido parece danino")
    a = ap.parse_args()
    global DANINO
    DANINO = a.danino
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
            activo = rpc("t_latido", **cred(), p_estado=est("libre"))
        except Exception as e:
            anotar("sin conexion:", e); time.sleep(60); continue
        if not activo or not en_horario() or hechas >= CFG["max_tareas_dia"]:
            if a.una_vez: return
            anotar("en pausa / fuera de horario / cupo del dia usado"); time.sleep(CFG["espera_sin_tareas_s"]); continue
        probar_cupo()
        if time.time() < LIMITE_HASTA:
            rpc("t_latido", **cred(), p_estado="limite_uso")
            if a.una_vez: return
            time.sleep(min(CFG["espera_sin_tareas_s"], 300)); continue
        responder_bandeja()
        opinar_propuestas()
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
