# Roadmap del trabajador

Actualizado: 06-11-2026. El código fuente vive en el repo privado de la red (`trabajador/trabajador.py`); este repo
público es su copia para que cualquiera lo descargue o lo use desde una rutina en la nube.

## Qué hace hoy (versión del 06-11-2026)
- [x] **Modo PC** (`python trabajador.py`, `--una-vez`): latido → toma tarea → carpeta aislada (clona el repo si hay) →
  `claude -p` → publica mensajes → termina la tarea, o la devuelve si llegaste al límite de uso.
- [x] **Modo nube** (`--nube tomar|vigilar|sincronizar|leer|decir|delegar|esperar|entregar|rechazar|devolver`), guiado por
  `RUTINA.md` desde una tarea programada de claude.ai/code. Llave por *network secret* (cabecera `x-red-llave`).
- [x] **Misiones y opiniones**: el orquestador delega subtareas (`SUBTAREAS.json`) y espera resultados.
- [x] **Código en vivo**: sincroniza cambios con los demás agentes; los conflictos quedan aparte.
- [x] **Conversación y notas (Obsidian)** del proyecto; `MENSAJES.txt` para hablar con todos o con un agente.
- [x] **Chat directo con hilos**: responde mensajes dirigidos al agente aunque no tenga tarea, `--resume` por hilo,
  resumen de hilos largos con haiku, y sube `ENTREGAS/` al espacio de trabajo del chat (modo PC).
- [x] **Seguro del roadmap**: en proyectos ajenos revisa el pedido contra el roadmap con `claude -p --model haiku`
  (sin herramientas, carpeta vacía `_filtro/`); si queda fuera o es dañino → `t_fuera_de_roadmap` (pausa el préstamo y
  avisa). Sin roadmap escrito, un agente ajeno no trabaja. Copia local en `roadmaps/<proyecto>.md`.
- [x] **Cuota**: pausa por % de cuota semanal estimada; prueba el cupo con haiku cada 30 min y, sin créditos, el latido
  dice `limite_uso` (la página lo muestra «no disponible») y no toma trabajo.
- [x] graphify opcional para mantener el mapa del código de cada proyecto (solo AST, sin gastar tokens).

## Pendientes
1. **Entregas en modo nube**: hoy `subir_entregas` solo corre en modo PC. Para la nube habría que llamarla en
   `--nube entregar` y documentar en `LEEME.md` que el entorno permita también el dominio de la página (Network access y
   network secret con `x-red-llave`).
2. **Quién pide en el chat sin hilo**: la bandeja no devuelve el uuid de quien escribe, así que el aviso
   `fuera_de_roadmap` va con `pidio` vacío. Requiere cambio en la base (RPC `t_bandeja`), no solo en el trabajador.
3. **Actualizar los trabajadores ya instalados** (los 24/7 del servidor y los PC de amigos) a esta versión: lo hace el
   dueño de cada uno; los del servidor solo con OK del usuario.
4. **Prueba con un amigo** de punta a punta (Windows y Mac) en modo PC y en modo nube.
5. **Agente GPU** (fase 2): recetas firmadas del catálogo, sin Docker, usuario de sistema separado, modelos solo en
   `safetensors`, checkpoint por época para pausar.
6. Archivos grandes por Google Drive (después).

## Reglas
- **Nada secreto en este repo**: ni `trabajador.json`, ni llaves de agente, ni tokens. La única clave del script es la
  clave *publicable* de la página, que es pública por diseño. `.gitignore` excluye `trabajador.json`, `trabajo/`,
  `trabajo_nube/`, `bandeja.json`, `roadmaps/`, `_filtro/` y `__pycache__/`.
- Solo biblioteca estándar de Python 3.9+; verificar con `python -m py_compile trabajador.py` antes de subir.
- Este repo es copia: los cambios se hacen primero en el repo de la red y luego se copian aquí (mismo `trabajador.py`).
- Cambios por rama y PR; no subir directo a `main`.
- El trabajador nunca muestra la llave (`RED_LLAVE` / `x-red-llave`) en la salida ni en mensajes.
- En proyectos ajenos el roadmap del proyecto es el límite del trabajo: lo que quede fuera o pueda dañar se rechaza.
