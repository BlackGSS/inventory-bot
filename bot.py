import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import unicodedata
import wave
from collections import Counter, OrderedDict
from functools import wraps

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "/data/inventario.db")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:3b")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_DIR = os.environ.get("WHISPER_DIR", "/models")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "es")
DEFAULT_CAT = "Sin categoría"
PAGE = 8  # objetos por página en las listas

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("inventario")


# ---------- Base de datos ----------
def norm(text: str) -> str:
    """Minúsculas y sin acentos, para buscar sin problemas."""
    t = unicodedata.normalize("NFD", text.strip().lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with db() as c:
        c.execute(
            """CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                name_norm TEXT NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                location TEXT NOT NULL,
                location_norm TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        # Migraciones: añaden columnas si la base de datos es de una versión anterior
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        if "category" not in cols:
            log.info("Migrando base de datos: añadiendo categoría")
            c.execute(f"ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT '{DEFAULT_CAT}'")
            c.execute(f"ALTER TABLE items ADD COLUMN category_norm TEXT NOT NULL DEFAULT '{norm(DEFAULT_CAT)}'")
        if "tags" not in cols:
            log.info("Migrando base de datos: añadiendo tags")
            c.execute("ALTER TABLE items ADD COLUMN tags TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE items ADD COLUMN tags_norm TEXT NOT NULL DEFAULT ''")
        if "description" not in cols:
            log.info("Migrando base de datos: añadiendo descripción")
            c.execute("ALTER TABLE items ADD COLUMN description TEXT NOT NULL DEFAULT ''")
            c.execute("ALTER TABLE items ADD COLUMN description_norm TEXT NOT NULL DEFAULT ''")
        c.execute("CREATE TABLE IF NOT EXISTS blocked_tags (tag_norm TEXT PRIMARY KEY, tag TEXT NOT NULL)")
        # Listas de resultados y códigos de botones: en disco, para que sigan funcionando tras reiniciar el bot
        c.execute("CREATE TABLE IF NOT EXISTS views (token TEXT PRIMARY KEY, ids TEXT NOT NULL, title TEXT NOT NULL)")
        c.execute("CREATE TABLE IF NOT EXISTS tag_tokens (token TEXT PRIMARY KEY, pairs TEXT NOT NULL)")


def get_item(item_id: int):
    with db() as c:
        return c.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()


def query_ids(where: str, params: tuple, order: str) -> list[int]:
    """Devuelve los ids de los objetos que cumplen la condición (where/order son fijos, no del usuario)."""
    with db() as c:
        return [r["id"] for r in c.execute(f"SELECT id FROM items WHERE {where} ORDER BY {order}", params)]


def save_tags(c, item_id: int, tags: list[str]):
    text = ", ".join(tags)
    c.execute("UPDATE items SET tags=?, tags_norm=? WHERE id=?", (text, norm(text), item_id))


# ---------- Gestión de tags ----------
def tag_counts():
    """Devuelve (contador por tag normalizado, etiqueta tal como se escribió)."""
    counts, labels = Counter(), {}
    with db() as c:
        for r in c.execute("SELECT tags FROM items WHERE tags != ''"):
            for t in r["tags"].split(","):
                t = t.strip()
                if t:
                    n = norm(t)
                    counts[n] += 1
                    labels.setdefault(n, t)
    return counts, labels


def ids_with_tag(tag_norm: str) -> list[int]:
    """Ids de los objetos que tienen exactamente ese tag."""
    with db() as c:
        rows = c.execute(
            "SELECT id, tags FROM items WHERE tags_norm LIKE ? ORDER BY name_norm", (f"%{tag_norm}%",)
        ).fetchall()
    return [r["id"] for r in rows if tag_norm in {norm(t) for t in r["tags"].split(",")}]


def count_items_with(norms: set) -> int:
    with db() as c:
        rows = c.execute("SELECT tags FROM items WHERE tags != ''").fetchall()
    return sum(1 for r in rows if any(norm(t) in norms for t in r["tags"].split(",")))


def remove_tags(norms: set) -> int:
    """Quita esos tags de todos los objetos. Devuelve cuántos objetos han cambiado."""
    changed = 0
    with db() as c:
        for r in c.execute("SELECT id, tags FROM items WHERE tags != ''").fetchall():
            parts = [t.strip() for t in r["tags"].split(",") if t.strip()]
            kept = [t for t in parts if norm(t) not in norms]
            if len(kept) != len(parts):
                save_tags(c, r["id"], kept)
                changed += 1
    return changed


def get_blocked() -> set:
    with db() as c:
        return {r["tag_norm"] for r in c.execute("SELECT tag_norm FROM blocked_tags")}


def block_tags(pairs):
    with db() as c:
        c.executemany("INSERT OR REPLACE INTO blocked_tags (tag_norm, tag) VALUES (?,?)", pairs)


def unblock_tags(norms):
    with db() as c:
        c.executemany("DELETE FROM blocked_tags WHERE tag_norm=?", [(n,) for n in norms])


def apply_manual_tags(row, raw: list[str]) -> list[str]:
    """Tags escritos a mano: se respetan siempre (y se desbloquean si estaban bloqueados)."""
    tags = clean_tags(raw, row["name"], row["category"], limit=12)
    with db() as c:
        save_tags(c, row["id"], tags)
    unblock_tags([norm(t) for t in tags])
    return tags


def tag_token(ctx, pairs) -> str:
    """Código corto para usar en los botones (Telegram limita su tamaño). Se guarda en disco."""
    tok = hashlib.md5("|".join(sorted(n for n, _ in pairs)).encode()).hexdigest()[:10]
    with db() as c:
        c.execute("INSERT OR REPLACE INTO tag_tokens (token, pairs) VALUES (?,?)", (tok, json.dumps(list(pairs))))
        c.execute("DELETE FROM tag_tokens WHERE rowid <= (SELECT MAX(rowid) FROM tag_tokens) - 5000")
    return tok


def lookup_tags(ctx, tok: str):
    with db() as c:
        row = c.execute("SELECT pairs FROM tag_tokens WHERE token=?", (tok,)).fetchone()
    return [tuple(p) for p in json.loads(row["pairs"])] if row else None


# ---------- IA local (Ollama) ----------
TAG_SCHEMA = {
    "type": "object",
    "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    "required": ["tags"],
}


def clean_tags(raw, name: str, cat: str, limit: int = 6, blocked=None) -> list[str]:
    """Limpia la respuesta del modelo: minúsculas, sin duplicados, sin el propio nombre ni tags bloqueados."""
    out, seen = [], {norm(name), norm(cat)} | set(blocked or ())
    for t in raw:
        t = re.sub(r"\s+", " ", str(t).strip().lower()).strip(" #.,;:-")
        n = norm(t)
        if t and len(t) <= 30 and n not in seen:
            seen.add(n)
            out.append(t)
    return out[:limit]


async def generate_tags(name: str, cat: str, note: str = ""):
    """Devuelve una lista de tags, o None si la IA no está disponible."""
    cat_line = "" if cat == DEFAULT_CAT else f'\nCategoría: "{cat}"'
    note_line = f'\nNota del usuario: "{note[:300]}"' if note else ""
    prompt = (
        f'Objeto: "{name}"{cat_line}{note_line}\n\n'
        "Genera entre 4 y 8 etiquetas (tags) en español, en minúsculas, de una o dos palabras, "
        "que describan para qué sirve el objeto, de qué tipo es o con qué se relaciona. "
        "No repitas el nombre del objeto."
    )
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "stream": False,
                    "format": TAG_SCHEMA,
                    "options": {"temperature": 0.2},
                    "messages": [
                        {
                            "role": "system",
                            "content": "Eres un asistente que etiqueta objetos de un inventario "
                            "de un estudio. Respondes solo con JSON.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                },
            )
            r.raise_for_status()
        data = json.loads(r.json()["message"]["content"])
        return clean_tags(data.get("tags", []), name, cat, blocked=get_blocked())
    except Exception as e:
        log.warning("No se pudieron generar tags: %s", e)
        return None


# ---------- Utilidades ----------
def authorized(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        if ALLOWED and user.id not in ALLOWED:
            log.warning("Acceso denegado a %s (%s)", user.id, user.username)
            await update.message.reply_text("⛔ No tienes permiso para usar este bot.")
            return
        return await func(update, ctx)

    return wrapper


def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def fmt(rows) -> str:
    """Formato compacto (para el comando /lista con ids y los comandos avanzados)."""
    lines = []
    for r in rows:
        line = f"{r['name']} ×{r['quantity']} → 📍 {r['location']} · 🏷️ {r['category']}"
        if r["tags"]:
            line += f"\n      🔖 {r['tags']}"
        lines.append(line)
    return "\n".join(lines)


async def reply_long(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i : i + 4000])


def args_text(ctx) -> str:
    return " ".join(ctx.args).strip()


def split_args(text: str, maxsplit: int = -1) -> list[str]:
    """Separa por comas. Si el texto contiene «|», se usa ese separador (útil si un nombre lleva comas)."""
    sep = "|" if "|" in text else ","
    return [p.strip() for p in text.split(sep, maxsplit)]


# ---------- Ficha de un objeto (con botones) ----------
MAX_NOTE_SHOWN = 1500  # caracteres de la nota que se muestran (el texto completo se conserva)


def short_note(text: str) -> str:
    return text if len(text) <= MAX_NOTE_SHOWN else text[:MAX_NOTE_SHOWN].rstrip() + "… (nota recortada)"


def card_text(r, note: str = "") -> str:
    lines = [note, ""] if note else []
    lines += [
        f"📦 {r['name']}  (#{r['id']})",
        f"Cantidad: {r['quantity']}",
        f"📍 {r['location']}",
        f"🏷️ {r['category']}",
        f"🔖 {r['tags']}" if r["tags"] else "🔖 (sin tags)",
    ]
    if r["description"]:
        lines.append(f"📝 {short_note(r['description'])}")
    return "\n".join(lines)


def card_keyboard(item_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [btn("➖ 1", f"q:{item_id}:-1"), btn("➕ 1", f"q:{item_id}:1"), btn("✏️ Cantidad", f"qs:{item_id}")],
            [btn("📍 Mover", f"mv:{item_id}"), btn("🏷️ Categoría", f"ct:{item_id}")],
            [btn("🔖 Tags con IA", f"tg:{item_id}"), btn("✏️ Tags", f"te:{item_id}")],
            [btn("📝 Nota", f"nt:{item_id}"), btn("🗑️ Borrar", f"dl:{item_id}")],
        ]
    )


def card_content(item_id: int, note: str = ""):
    r = get_item(item_id)
    if not r:
        return None
    return card_text(r, note), card_keyboard(item_id)


async def send_card(message, item_id: int, note: str = ""):
    content = card_content(item_id, note)
    if not content:
        await message.reply_text("🤷 Ese objeto ya no existe.")
        return
    return await message.reply_text(content[0], reply_markup=content[1])


async def refresh_card(q, item_id: int, note: str = ""):
    content = card_content(item_id, note)
    if not content:
        await q.edit_message_text("🤷 Ese objeto ya no existe.")
        return
    await q.edit_message_text(content[0], reply_markup=content[1])


# ---------- Listas paginadas (con botones) ----------
VIEW_KEEP = 1000  # listas que se recuerdan (las más recientes); no dependen del tiempo ni de reinicios


def store_view(ctx, ids: list[int], title: str) -> str:
    """Guarda una lista de resultados en la base de datos para poder paginarla cuando quieras."""
    token = secrets.token_hex(4)
    with db() as c:
        c.execute("INSERT OR REPLACE INTO views (token, ids, title) VALUES (?,?,?)", (token, json.dumps(ids), title))
        c.execute("DELETE FROM views WHERE rowid <= (SELECT MAX(rowid) FROM views) - ?", (VIEW_KEEP,))
    return token


def view_content(ctx, token: str, page: int):
    with db() as c:
        row = c.execute("SELECT ids, title FROM views WHERE token=?", (token,)).fetchone()
        if not row:
            return None
        ids, title = json.loads(row["ids"]), row["title"]
        rows = c.execute(
            f"SELECT * FROM items WHERE id IN ({','.join('?' * len(ids))})", ids
        ).fetchall() if ids else []
    by_id = {r["id"]: r for r in rows}
    rows = [by_id[i] for i in ids if i in by_id]  # mantiene el orden original

    pages = max(1, (len(rows) + PAGE - 1) // PAGE)
    page = max(0, min(page, pages - 1))
    chunk = rows[page * PAGE : (page + 1) * PAGE]

    head = f"{title} — {len(rows)} objeto(s)"
    if pages > 1:
        head += f" · pág. {page + 1}/{pages}"
    text = head + "\n\n" + "\n".join(
        f"• {r['name']} ×{r['quantity']} → 📍 {r['location']} · 🏷️ {r['category']}" for r in chunk
    )
    kb = [[btn(f"{r['name']} ×{r['quantity']}"[:40], f"it:{r['id']}")] for r in chunk]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(btn("◀", f"pg:{token}:{page - 1}"))
        nav.append(btn(f"{page + 1}/{pages}", "noop"))
        if page < pages - 1:
            nav.append(btn("▶", f"pg:{token}:{page + 1}"))
        kb.append(nav)
    return text, InlineKeyboardMarkup(kb)


async def show_view(message, ctx, ids: list[int], title: str, empty: str):
    if not ids:
        await message.reply_text(empty)
        return
    if len(ids) == 1:  # un solo resultado: directamente su ficha
        await send_card(message, ids[0])
        return
    token = store_view(ctx, ids, title)
    text, kb = view_content(ctx, token, 0)
    await message.reply_text(text, reply_markup=kb)


async def show_groups(message, col: str, icon: str, cb: str, empty: str):
    """Muestra lugares o categorías como botones (col: 'location' o 'category')."""
    with db() as c:
        rows = c.execute(
            f"SELECT {col} AS label, MIN(id) AS rep, COUNT(*) AS n FROM items "
            f"GROUP BY {col}_norm ORDER BY {col}_norm"
        ).fetchall()
    if not rows:
        await message.reply_text(empty)
        return
    shown = rows[:90]  # Telegram admite como máximo 100 botones
    btns = [btn(f"{icon} {r['label']} ({r['n']})"[:40], f"{cb}:{r['rep']}") for r in shown]
    kb = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    text = f"{icon} {len(rows)} en total. Toca uno para ver su contenido:"
    if len(rows) > len(shown):
        text += f"\n(Se muestran {len(shown)}; usa /lugar o /categoria con texto para buscar el resto)"
    await message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


# ---------- Comandos ----------
HELP = (
    "📦 *Inventario del estudio*\n\n"
    "*Añadir*\n"
    "/add nombre, cantidad, lugar, categoría, tags: uno, dos, nota: texto\n"
    "   ej: `/add Lápiz, 10, Cajón 2, Colegio, tags: papelería, nota: los de dibujo técnico`\n"
    "   Cantidad, categoría, tags y nota son opcionales:\n"
    "   `/add Taladro, Armario`\n"
    "   `/add Taladro, Armario, Herramientas`\n"
    "   `/add Taladro, Armario, tags: bricolaje, taladrar`\n"
    "   (si el nombre lleva comas, separa con | )\n"
    "   Si pones tags, se usan los tuyos; si no, los genera la IA local.\n\n"
    "*Añadir por voz* 🎤 (o escribiéndolo así, sin comando)\n"
    "   «Añade tornillos en el cajón 2, tengo 50, a la categoría ferretería»\n"
    "   Con tags y nota: «Añade pegatinas en el cajón 2, con tags decoración, con la nota son de dinosaurios»\n"
    "   (la nota va siempre al final)\n"
    "   «Tengo», «a la categoría», «con tags» y «con la nota» son opcionales. Te enseño lo que he entendido para que confirmes o edites.\n\n"
    "*Consultar* (salen con botones: toca un objeto para editarlo)\n"
    "/busca texto – por nombre, tag o nota\n"
    "/lugares – elige un lugar\n"
    "/categorias – elige una categoría\n"
    "/lugar texto · /categoria texto · /tag texto\n"
    "/tags – ver tags y eliminar los que no quieras\n"
    "/borrartag tag1, tag2 – eliminar tags de todos los objetos\n"
    "/bloqueados – tags que la IA ya no genera (puedes permitirlos)\n"
    "/lista – todo el inventario, paginado\n\n"
    "*Editar*\n"
    "Toca un objeto en los resultados y usa sus botones: cantidad, mover, categoría, "
    "tags (con IA o a mano) y borrar.\n\n"
    "*Otros*\n"
    "/retag todos – generar tags a los que no tienen\n"
    "/voz – estado del reconocimiento de voz (y reintentar la carga)\n"
    "/cancelar – cancelar una edición a medias (si no contestas, caduca a los 5 minutos)\n\n"
    "*Avanzado (por id)*\n"
    "/cantidad id n · /mover id, lugar · /setcat id, categoría\n"
    "/settags id, a, b, c · /borrar id"
)


@authorized
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


@authorized
async def cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.pop("pending", None)
    await update.message.reply_text("👍 Cancelado.")


TAGS_KEY_RE = re.compile(r"(?:^|[\s,|])(?:tags?|etiquetas?)\s*:\s*", re.I)
NOTE_KEY_RE = re.compile(r"(?:^|[\s,|])(?:notas?|descripci[oó]n|desc)\s*:\s*", re.I)


def split_add_args(text: str):
    """Separa «datos, tags: a, b, nota: texto» en (datos, tags, nota). Tags y nota pueden ir en cualquier
    orden; la nota puede llevar comas (llega hasta el siguiente marcador o el final)."""
    marks = []
    for kind, rx in (("tags", TAGS_KEY_RE), ("note", NOTE_KEY_RE)):
        m = rx.search(text)
        if m:
            marks.append((m.start(), m.end(), kind))
    marks.sort()
    fields = text[: marks[0][0]] if marks else text
    segs = {}
    for i, (_, en, kind) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        segs[kind] = text[en:end]
    tags = split_tags(segs.get("tags", ""))
    note = segs.get("note", "").strip(" ,|\n")
    return fields.rstrip(" ,|"), tags, note


def split_tags(text: str, voice: bool = False) -> list[str]:
    """Separa una lista de tags por comas. Con voice=True, en el último también separa «a y b»
    (así se enumera al hablar: «papelería, escribir y dibujo»)."""
    items = [t.strip(" .;:!?¿¡") for t in re.split(r"[,;|]", text)]
    items = [t for t in items if t]
    if voice and items:
        items = items[:-1] + [t.strip() for t in re.split(r"\s+y\s+", items[-1]) if t.strip()]
    return items


async def save_item(message, name: str, qty: int, place: str, cat: str, tags=None, note: str = ""):
    """Guarda un objeto (o suma cantidad si ya existe).

    Si se indican tags, se usan esos; solo cuando no hay ninguno los genera la IA local."""
    tags = [t.strip() for t in (tags or []) if t and t.strip()]
    note = (note or "").strip()
    # 1) Guardar en la base de datos (rápido)
    merged = None
    item_id = None
    with db() as c:
        row = c.execute(
            "SELECT * FROM items WHERE name_norm=? AND location_norm=?",
            (norm(name), norm(place)),
        ).fetchone()
        if row:  # mismo objeto en el mismo sitio: sumamos
            c.execute("UPDATE items SET quantity=quantity+? WHERE id=?", (qty, row["id"]))
            if cat:
                c.execute(
                    "UPDATE items SET category=?, category_norm=? WHERE id=?",
                    (cat, norm(cat), row["id"]),
                )
            if note and norm(note) not in row["description_norm"]:  # la nota nueva se suma a la que hubiera
                desc = f"{row['description']}\n{note}" if row["description"] else note
                c.execute(
                    "UPDATE items SET description=?, description_norm=? WHERE id=?",
                    (desc, norm(desc), row["id"]),
                )
            merged = (row["id"], row["quantity"] + qty)
        else:
            cat = cat or DEFAULT_CAT
            cur = c.execute(
                """INSERT INTO items
                   (name, name_norm, quantity, location, location_norm, category, category_norm,
                    description, description_norm)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (name, norm(name), qty, place, norm(place), cat, norm(cat), note, norm(note)),
            )
            item_id = cur.lastrowid

    if merged:
        note_msg = f"➕ Ya existía: ahora hay {merged[1]}"
        if note:
            note_msg += " · 📝 nota añadida"
        if tags:  # los nuevos se suman a los que ya tuviera
            existing = get_item(merged[0])
            current = [t.strip() for t in existing["tags"].split(",") if t.strip()]
            apply_manual_tags(existing, current + tags)
            note_msg += " · 🔖 tags añadidos"
        await send_card(message, merged[0], note=note_msg)
        return

    if tags:  # tags indicados por el usuario: no hace falta la IA
        saved = apply_manual_tags(get_item(item_id), tags)
        if saved:
            await send_card(message, item_id, note="✅ Guardado (con tus tags)")
            return
        # los tags indicados no eran válidos (p. ej. repetían el nombre): se deja a la IA

    msg = await send_card(message, item_id, note="✅ Guardado · ⏳ generando tags...")

    # 2) Generar tags con la IA local (puede tardar unos segundos)
    ai_tags = await generate_tags(name, cat, note)
    if ai_tags:
        with db() as c:
            save_tags(c, item_id, ai_tags)
        note = "✅ Guardado"
    elif ai_tags is None:
        note = "✅ Guardado · ⚠️ La IA no está disponible (puedes usar «Tags con IA» más tarde)"
    else:
        note = "✅ Guardado · la IA no propuso tags"
    content = card_content(item_id, note)
    if content and msg:
        await msg.edit_text(content[0], reply_markup=content[1])


@authorized
async def add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, tags, note = split_add_args(args_text(ctx))  # «tags:» y «nota:» son opcionales
    parts = split_args(text)
    try:
        name = parts[0]
        has_qty = len(parts) > 1 and parts[1].isdigit()
        if len(parts) > (4 if has_qty else 3):
            raise ValueError  # demasiadas partes: probablemente el nombre lleva comas
        if has_qty:
            # nombre, cantidad, lugar [, categoría]
            qty, place = int(parts[1]), parts[2]
            cat = parts[3] if len(parts) > 3 else ""
        else:
            # nombre, lugar [, categoría]
            qty, place = 1, parts[1]
            cat = parts[2] if len(parts) > 2 else ""
        if not name or not place or qty < 1:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Formato: /add nombre, cantidad, lugar, categoría, tags: uno, dos, nota: texto\n"
            "Ej: /add Cinta métrica, 2, Cajón 1, Herramientas, tags: medir, nota: la de 5 metros\n"
            "(Cantidad, categoría, tags y nota son opcionales; si no pones tags, los genera la IA)\n"
            "(Si el nombre lleva comas, separa con | en vez de comas)"
        )
        return

    await save_item(update.message, name, qty, place, cat, tags, note)

@authorized
async def buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = args_text(ctx)
    q = norm(text)
    if not q:
        await update.message.reply_text("Uso: /busca texto")
        return
    ids = query_ids(
        "name_norm LIKE ? OR tags_norm LIKE ? OR description_norm LIKE ?", (f"%{q}%", f"%{q}%", f"%{q}%"), "name_norm"
    )
    await show_view(update.message, ctx, ids, f"🔎 «{text}»", "🤷 No encuentro nada con ese nombre, tag o nota.")


@authorized
async def lugar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = args_text(ctx)
    q = norm(text)
    if not q:
        await update.message.reply_text("Uso: /lugar texto   (o /lugares para elegir de una lista)")
        return
    ids = query_ids("location_norm LIKE ?", (f"%{q}%",), "location_norm, name_norm")
    await show_view(update.message, ctx, ids, f"📍 «{text}»", "🤷 No hay nada en ese lugar.")


@authorized
async def categoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = args_text(ctx)
    q = norm(text)
    if not q:
        await update.message.reply_text("Uso: /categoria texto   (o /categorias para elegir de una lista)")
        return
    ids = query_ids("category_norm LIKE ?", (f"%{q}%",), "category_norm, name_norm")
    await show_view(update.message, ctx, ids, f"🏷️ «{text}»", "🤷 No hay nada en esa categoría.")


@authorized
async def tag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = args_text(ctx)
    q = norm(text)
    if not q:
        await update.message.reply_text("Uso: /tag texto\n(mira /tags para ver los que existen)")
        return
    ids = query_ids("tags_norm LIKE ?", (f"%{q}%",), "name_norm")
    await show_view(update.message, ctx, ids, f"🔖 «{text}»", "🤷 No hay nada con ese tag.")


@authorized
async def lugares(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_groups(update.message, "location", "📍", "pl", "Inventario vacío.")


@authorized
async def categorias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await show_groups(update.message, "category", "🏷️", "ca", "Inventario vacío.")


TAGS_PER_PAGE = 20


def tags_page_content(ctx, page: int, mode: str):
    counts, labels = tag_counts()
    if not counts:
        return None
    order = sorted(counts, key=(lambda n: (counts[n], n)) if mode == "r" else (lambda n: n))
    pages = max(1, (len(order) + TAGS_PER_PAGE - 1) // TAGS_PER_PAGE)
    page = max(0, min(page, pages - 1))
    chunk = order[page * TAGS_PER_PAGE : (page + 1) * TAGS_PER_PAGE]
    btns = [
        btn(f"🔖 {labels[n]} ({counts[n]})"[:40], f"ts:{tag_token(ctx, [(n, labels[n])])}") for n in chunk
    ]
    kb = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    nav = []
    if page > 0:
        nav.append(btn("◀", f"tp:{mode}:{page - 1}"))
    nav.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("▶", f"tp:{mode}:{page + 1}"))
    kb.append(nav)
    kb.append([btn("↕️ Orden A-Z" if mode == "r" else "↕️ Menos usados primero", f"tp:{'a' if mode == 'r' else 'r'}:0")])
    orden = "menos usados primero" if mode == "r" else "A-Z"
    text = f"🔖 {len(order)} tags (orden: {orden}). Toca uno para ver sus objetos o eliminarlo:"
    return text, InlineKeyboardMarkup(kb)


def delete_confirm(pairs, tok: str, n_items: int):
    names = ", ".join(f"«{l}»" for _, l in pairs)
    text = (
        f"¿Eliminar {names} de {n_items} objeto(s)?\n\n"
        "«No volver a generar» evita que la IA los vuelva a proponer en objetos nuevos."
    )
    kb = InlineKeyboardMarkup(
        [
            [btn("🗑️ Eliminar y no volver a generar", f"tdy:1:{tok}")],
            [btn("🗑️ Solo eliminar", f"tdy:0:{tok}")],
            [btn("Cancelar", f"tc:{tok}")],
        ]
    )
    return text, kb


def blocked_content(ctx):
    with db() as c:
        rows = c.execute("SELECT tag_norm, tag FROM blocked_tags ORDER BY tag_norm").fetchall()
    if not rows:
        return "✅ No hay tags bloqueados.", None
    btns = [btn(f"🚫 {r['tag']}"[:40], f"ub:{tag_token(ctx, [(r['tag_norm'], r['tag'])])}") for r in rows[:90]]
    kb = [btns[i : i + 2] for i in range(0, len(btns), 2)]
    return "🚫 Tags que la IA no genera. Toca uno para permitirlo de nuevo:", InlineKeyboardMarkup(kb)


@authorized
async def tags_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    content = tags_page_content(ctx, 0, "a")
    if not content:
        await update.message.reply_text("Todavía no hay tags.")
        return
    await update.message.reply_text(content[0], reply_markup=content[1])


@authorized
async def borrartag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    raw = [t.strip() for t in args_text(ctx).split(",") if t.strip()]
    if not raw:
        await update.message.reply_text(
            "Uso: /borrartag tag1, tag2\n(o abre /tags y elige de la lista)"
        )
        return
    counts, labels = tag_counts()
    found, missing = [], []
    for t in raw:
        n = norm(t)
        if n in counts:
            if (n, labels[n]) not in found:
                found.append((n, labels[n]))
        else:
            similar = [labels[k] for k in counts if n in k][:5]
            missing.append(f"«{t}»" + (f" (¿quizá {', '.join(similar)}?)" if similar else ""))
    if missing:
        await update.message.reply_text("🤷 No encuentro estos tags: " + "; ".join(missing))
    if not found:
        return
    tok = tag_token(ctx, found)
    text, kb = delete_confirm(found, tok, count_items_with({n for n, _ in found}))
    await update.message.reply_text(text, reply_markup=kb)


@authorized
async def bloqueados(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text, kb = blocked_content(ctx)
    await update.message.reply_text(text, reply_markup=kb)


@authorized
async def lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ids = query_ids("1=1", (), "category_norm, name_norm")
    await show_view(update.message, ctx, ids, "📋 Inventario", "Inventario vacío.")


# ---------- Voz y lenguaje natural ----------
UNITS = {
    "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
    "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15,
    "dieciseis": 16, "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "veintiun": 21,
    "veintiuno": 21, "veintiuna": 21, "veintidos": 22, "veintitres": 23, "veinticuatro": 24,
    "veinticinco": 25, "veintiseis": 26, "veintisiete": 27, "veintiocho": 28, "veintinueve": 29,
}
TENS = {"treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90}
HUNDREDS = {
    "cien": 100, "ciento": 100,
    "doscientos": 200, "doscientas": 200, "trescientos": 300, "trescientas": 300,
    "cuatrocientos": 400, "cuatrocientas": 400, "quinientos": 500, "quinientas": 500,
    "seiscientos": 600, "seiscientas": 600, "setecientos": 700, "setecientas": 700,
    "ochocientos": 800, "ochocientas": 800, "novecientos": 900, "novecientas": 900,
}
ARTICLES = {"el", "la", "los", "las", "un", "una", "unos", "unas"}
TRAILING = {"que", "y", "pero", "ya", "tambien", "ademas"}

ADD_VERB = re.compile(
    r"^\W*(?:(?:por favor|oye|vale|hey|bueno)\W+)*"
    r"(?:(?:quiero|puedes|podr[ií]as|vamos a)\s+)?"
    r"(?:agr[eé]game|agrega|agregar|a[ñn][aá]deme|a[ñn]ade|a[ñn]adir|apunta|anota|registra|guarda|mete|ponme|pon)\b\W*",
    re.I,
)
CAT_RE = re.compile(r"\b(?:(?:a|en|de|para)\s+(?:la\s+)?)?categor[ií]a\s+(?:de\s+|d[eo]l\s+)?", re.I)
QTY_RE = re.compile(r"\b(?:tengo|tenemos|hay)\s+", re.I)
LOC_RE = re.compile(r"\b(?:dentro\s+d(?:e|el)\s+|en\s+)", re.I)
NOTE_RE = re.compile(
    r"\b(?:con\s+(?:(?:la|una)\s+)?(?:notas?|descripci[oó]n)\b\s*:?\s*|(?:notas?|descripci[oó]n)\s*:\s*)", re.I
)
TAGS_RE = re.compile(r"\b(?:con\s+(?:(?:los|las)\s+)?(?:tags?|etiquetas?)\b\s*:?\s*|(?:tags?|etiquetas?)\s*:\s*)", re.I)

EXAMPLE = "«Añade tornillos en el cajón 2, tengo 50, a la categoría ferretería»"


def nw(word: str) -> str:
    return norm(word.strip(".,;:!?¿¡"))


def parse_number(words: list[str]):
    """Lee un número (en cifras o en palabras) al principio de la lista. Devuelve (valor, palabras usadas)."""
    if not words:
        return None, 0
    if words[0].isdigit():
        return int(words[0]), 1
    total, i = 0, 0
    if words[i] in HUNDREDS:
        total += HUNDREDS[words[i]]
        i += 1
    if i < len(words) and words[i] in TENS:
        total += TENS[words[i]]
        i += 1
        if i + 1 < len(words) and words[i] == "y" and 1 <= UNITS.get(words[i + 1], 0) <= 9:
            total += UNITS[words[i + 1]]
            i += 2
    elif i < len(words) and words[i] in UNITS:
        total += UNITS[words[i]]
        i += 1
    return (total, i) if i else (None, 0)


def digitize(text: str) -> str:
    """«cajón dos» -> «cajón 2», para que los lugares se escriban siempre igual."""
    words = text.split()
    out, i = [], 0
    while i < len(words):
        v, used = parse_number([nw(w) for w in words[i:]])
        if v is not None and (v >= 2 or (nw(words[i]) == "uno" and i == len(words) - 1)):
            out.append(str(v))
            i += used
        else:
            out.append(words[i])
            i += 1
    return " ".join(out)


def clean_seg(text: str) -> str:
    text = text.strip(" \t\n.,;:!?¿¡\"'«»")
    words = text.split()
    while words and nw(words[-1]) in TRAILING:
        words.pop()
    return " ".join(words).strip(" .,;:!?")


def strip_article(text: str) -> str:
    parts = text.split(None, 1)
    return parts[1] if len(parts) == 2 and norm(parts[0]) in ARTICLES else text


def cap(text: str) -> str:
    return text[:1].upper() + text[1:]


def parse_add_phrase(text: str):
    """Interpreta frases como «Añade tornillos en el cajón 2, tengo 50, a la categoría ferretería».

    Devuelve (datos, None) si la entiende; (None, mensaje) si empieza como una orden de añadir pero falta
    algo; y (None, None) si no es una orden de añadir."""
    text = text.strip()
    m = ADD_VERB.match(text)
    if not m:
        return None, None
    rest = text[m.end():]

    found = {}
    # La nota es texto libre y va al final: lo que dice (con «en», «tengo»…) no se interpreta como datos.
    note_m = NOTE_RE.search(rest)
    head_end = note_m.start() if note_m else len(rest)
    if note_m:
        found["note"] = note_m
    cat_m = CAT_RE.search(rest, 0, head_end)
    if cat_m:
        found["cat"] = cat_m
    qty_m = QTY_RE.search(rest, 0, head_end)
    if qty_m:
        found["qty"] = qty_m
    for lm in LOC_RE.finditer(rest, 0, head_end):  # el primer «en» que no sea el de «en la categoría»
        if not (cat_m and cat_m.start() <= lm.start() < cat_m.end()):
            found["loc"] = lm
            break

    tags_m = TAGS_RE.search(rest)
    if tags_m:
        found["tags"] = tags_m

    def layout():
        marks = sorted(found.items(), key=lambda kv: kv[1].start())
        segs = {}
        for i, (kind, mk) in enumerate(marks):
            end = marks[i + 1][1].start() if i + 1 < len(marks) else len(rest)
            segs[kind] = rest[mk.end():end]
        return marks, segs

    marks, segs = layout()
    for kind in ("tags", "note"):
        if kind in segs and not clean_seg(segs[kind]):  # «rollo con etiquetas en el cajón»: era parte del nombre
            del found[kind]
    marks, segs = layout()

    name = clean_seg(rest[: marks[0][1].start()] if marks else rest)
    lead_qty = None
    words = name.split()
    v, used = parse_number([nw(w) for w in words])
    if v and used < len(words):  # «cinco rotuladores», «una caja de tornillos»
        lead_qty, name = v, " ".join(words[used:])
    name = cap(strip_article(name))
    place = cap(digitize(strip_article(clean_seg(segs.get("loc", "")))))
    cat = cap(digitize(strip_article(clean_seg(segs.get("cat", "")))))

    qty = None
    if "qty" in segs:
        qty, _ = parse_number([nw(w) for w in clean_seg(segs["qty"]).split()])
    qty = qty or lead_qty or 1

    if not name:
        return None, f"🤷 No he entendido qué objeto añadir. Di, por ejemplo: {EXAMPLE}."
    if not place:
        hint = " Recuerda que la nota va siempre al final de la frase." if "note" in found else ""
        return None, f"🤷 No he entendido dónde guardarlo.{hint} Di, por ejemplo: {EXAMPLE}."
    tags = [t.lower() for t in split_tags(clean_seg(segs.get("tags", "")), voice=True)]
    note = cap(clean_seg(segs.get("note", "")))
    return {"name": name, "qty": qty, "location": place, "category": cat, "tags": tags, "note": note}, None


def store_proposal(ctx, data: dict) -> str:
    props = ctx.bot_data.setdefault("proposals", OrderedDict())
    tok = secrets.token_hex(4)
    props[tok] = data
    while len(props) > 50:
        props.popitem(last=False)
    return tok


def proposal_content(tok: str, p: dict):
    edited = " · ✏️ editado" if p.get("edited") else ""
    text = (
        f"{p['icon']} «{p['heard']}»{edited}\n\n"
        f"📦 {p['name']} ×{p['qty']}\n"
        f"📍 {p['location']}\n"
        f"🏷️ {p['category'] or DEFAULT_CAT}\n"
        f"🔖 {', '.join(p['tags']) if p.get('tags') else '(los generará la IA al guardar)'}\n"
        f"{'📝 ' + short_note(p['note']) + chr(10) if p.get('note') else ''}\n"
        "¿Lo guardo?"
    )
    kb = InlineKeyboardMarkup(
        [
            [btn("✅ Guardar", f"vy:{tok}"), btn("✏️ Editar", f"ve:{tok}")],
            [btn("❌ Cancelar", f"vn:{tok}")],
        ]
    )
    return text, kb


def edit_menu_content(tok: str, p: dict):
    text = (
        "✏️ ¿Qué quieres cambiar?\n\n"
        f"📦 {p['name']} ×{p['qty']}\n"
        f"📍 {p['location']}\n"
        f"🏷️ {p['category'] or DEFAULT_CAT}\n"
        f"🔖 {', '.join(p['tags']) if p.get('tags') else '(los generará la IA al guardar)'}"
        + (f"\n📝 {short_note(p['note'])}" if p.get("note") else "")
    )
    kb = InlineKeyboardMarkup(
        [
            [btn("📦 Nombre", f"vf:{tok}:n"), btn("🔢 Cantidad", f"vf:{tok}:q")],
            [btn("📍 Lugar", f"vf:{tok}:l"), btn("🏷️ Categoría", f"vf:{tok}:c")],
            [btn("🔖 Tags", f"vf:{tok}:t"), btn("📝 Nota", f"vf:{tok}:d")],
            [btn("↩️ Volver", f"vb:{tok}")],
        ]
    )
    return text, kb


async def propose_add(message, ctx, res, heard, icon, replace=None, tok=None):
    """Enseña lo que se ha entendido y pide confirmación antes de guardar (con opción de editar)."""
    if tok is None:
        tok = store_proposal(ctx, {**res, "heard": heard, "icon": icon})
    p = ctx.bot_data["proposals"][tok]
    text, kb = proposal_content(tok, p)
    if replace:
        await replace.edit_text(text, reply_markup=kb)
        p["msg"] = replace
    else:
        p["msg"] = await message.reply_text(text, reply_markup=kb)


LEAD_RE = re.compile(
    r"^(?:es|era|son|tengo|hay|en|dentro\s+d(?:e|el)|(?:a|en)\s+la\s+categor[ií]a(?:\s+de)?|categor[ií]a)\s+", re.I
)
FIELD_NAMES = {
    "n": "el nombre correcto",
    "q": "la cantidad",
    "l": "el lugar",
    "c": "la categoría",
    "t": "los tags (separados por comas; di «ninguno» para que los genere la IA)",
    "d": "la nota o descripción (di «ninguna» para quitarla)",
}


def apply_prop_edit(prop: dict, field: str, raw: str):
    """Aplica una corrección (escrita o dictada) a un objeto pendiente de confirmar. Devuelve un error o None."""
    if field == "t":
        raw_t = re.sub(r"^\s*(?:tags?|etiquetas?)\s*:\s*", "", raw.strip(), flags=re.I)
        if norm(clean_seg(raw_t)) in ("ninguno", "ninguna", "sin tags", "sin etiquetas"):
            prop["tags"] = []  # sin tags propios: los generará la IA al guardar
        else:
            tags = [t.lower() for t in split_tags(raw_t, voice=True)]
            if not tags:
                return "No he entendido los tags."
            prop["tags"] = tags
        prop["edited"] = True
        return None
    if field == "d":
        raw_d = re.sub(r"^\s*(?:notas?|descripci[oó]n)\s*:\s*", "", raw.strip(), flags=re.I)
        if norm(clean_seg(raw_d)) in ("ninguna", "ninguno", "sin nota"):
            prop["note"] = ""
        else:
            val = cap(raw_d.strip())
            if not val:
                return "No he entendido la nota."
            prop["note"] = val
        prop["edited"] = True
        return None
    text = LEAD_RE.sub("", clean_seg(raw), count=1)
    if field == "q":
        v, _ = parse_number([nw(w) for w in text.split()])
        if not v:
            return "No he entendido la cantidad, dime un número."
        prop["qty"] = v
    elif field == "n":
        val = cap(strip_article(text))
        if not val:
            return "No he entendido el nombre."
        prop["name"] = val
    else:
        key = "location" if field == "l" else "category"
        if key == "category" and norm(text) in ("sin categoria", "ninguna", "ninguno"):
            val = ""
        else:
            val = cap(digitize(strip_article(text)))
            if not val:
                return f"No he entendido {FIELD_NAMES[field]}."
        prop[key] = val
    prop["edited"] = True
    return None


EDIT_TIMEOUT = 300  # segundos que el bot espera una corrección antes de olvidarla


EXPIRED_NOTE = (
    f"⌛ La edición anterior caducó (más de {EDIT_TIMEOUT // 60} minutos sin respuesta). "
    "Si querías cambiar algo, pulsa de nuevo el botón correspondiente."
)


def set_pending(ctx, **fields):
    """Deja al bot esperando una respuesta (escrita o dictada) del usuario."""
    ctx.user_data["pending"] = {**fields, "ts": time.monotonic()}


def renew_pending(ctx, pend: dict):
    """Vuelve a esperar la misma respuesta (p. ej. tras un dato inválido) y reinicia el plazo."""
    pend["ts"] = time.monotonic()
    ctx.user_data["pending"] = pend


def get_pending(ctx, consume: bool = False):
    """Devuelve (espera_vigente, caducada).

    - Una espera de un resumen que ya se guardó/canceló se descarta en silencio.
    - Si pasaron más de EDIT_TIMEOUT segundos sin respuesta, se descarta y caducada=True.
    - consume=True la retira al devolverla."""
    pend = ctx.user_data.get("pending")
    if not pend:
        return None, False
    if pend["kind"] == "pf" and pend["tok"] not in ctx.bot_data.get("proposals", {}):
        ctx.user_data.pop("pending", None)
        return None, False
    if time.monotonic() - pend.get("ts", 0) > EDIT_TIMEOUT:
        ctx.user_data.pop("pending", None)
        return None, True
    if consume:
        ctx.user_data.pop("pending", None)
    return pend, False


async def ask_prop_value(ctx, msg, tok: str, field: str):
    ctx.bot_data["proposals"][tok]["msg"] = msg
    set_pending(ctx, kind="pf", tok=tok, field=field)
    await msg.reply_text(
        f"✏️ Escribe {FIELD_NAMES[field]}, o mándame una nota de voz con ello (o /cancelar)."
    )


async def finish_prop_edit(ctx, pending: dict, raw: str, reply_to):
    """Aplica la corrección y enseña de nuevo el resumen. Devuelve un mensaje de error o None."""
    tok = pending["tok"]
    prop = ctx.bot_data.get("proposals", {}).get(tok)
    if not prop:
        return "Esto ya se procesó o caducó. Vuelve a dictarlo."
    err = apply_prop_edit(prop, pending["field"], raw)
    if err:
        return err
    old = prop.get("msg")
    if old:  # el resumen antiguo queda sin botones, para no confundirse
        try:
            await old.edit_text("✏️ Corregido: mira el mensaje de abajo.")
        except Exception:
            pass
    await propose_add(reply_to, ctx, None, None, None, tok=tok)
    return None


# Identificador del código que se está ejecutando (cambia si cambia bot.py): sirve para comprobar
# que el contenedor se ha reconstruido con el archivo nuevo.
with open(__file__, "rb") as _f:
    CODE_ID = hashlib.md5(_f.read()).hexdigest()[:8]


def lib_versions() -> str:
    """Versiones instaladas de las librerías de voz (para diagnosticar incompatibilidades)."""
    from importlib import metadata

    out = []
    for pkg in ("faster-whisper", "av", "ctranslate2", "huggingface-hub", "onnxruntime", "requests"):
        try:
            out.append(f"{pkg} {metadata.version(pkg)}")
        except Exception:
            out.append(f"{pkg} (no instalado)")
    return ", ".join(out)


_whisper = None
_whisper_lock = threading.Lock()
_whisper_state = "sin cargar"  # "sin cargar" | "cargando" | "listo" | "error"
_whisper_error = None  # última excepción al cargar el modelo
_whisper_warning = None  # aviso si falló la prueba de audio al arrancar


def check_voice_pipeline():
    """Prueba de humo: pasa 1 s de silencio por la misma decodificación y detección de voz que un audio real.
    Así una incompatibilidad entre librerías sale en el log al arrancar, y no al mandar el primer audio."""
    from faster_whisper.audio import decode_audio
    from faster_whisper.vad import get_speech_timestamps

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    try:
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(b"\x00\x00" * 16000)
        audio = decode_audio(path, sampling_rate=16000)
        get_speech_timestamps(audio)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def patch_av_compat():
    """faster-whisper 1.1.0 abre el audio con av.open(..., metadata_errors="ignore"), un parámetro que algunas
    versiones de PyAV ya no aceptan. Si PyAV lo rechaza, se reintenta sin él (solo cambia cómo se tratan
    metadatos defectuosos del archivo). Así funciona con cualquier versión de PyAV."""
    try:
        import av
    except ImportError:
        return
    if getattr(av.open, "_inventario_compat", False):
        return  # ya parcheado
    original = av.open

    def open_compat(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        except TypeError as e:
            if "metadata_errors" not in str(e) or "metadata_errors" not in kwargs:
                raise
            kwargs.pop("metadata_errors")
            return original(*args, **kwargs)

    open_compat._inventario_compat = True
    av.open = open_compat


def get_whisper():
    """Carga el modelo de voz la primera vez (la primera vez también lo descarga)."""
    global _whisper, _whisper_state, _whisper_error, _whisper_warning
    with _whisper_lock:
        if _whisper is None:
            _whisper_state = "cargando"
            try:
                from faster_whisper import WhisperModel

                patch_av_compat()
                log.info("Cargando modelo de voz «%s»...", WHISPER_MODEL)
                model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", download_root=WHISPER_DIR)
            except Exception as e:
                _whisper_state, _whisper_error = "error", e
                raise
            _whisper, _whisper_state, _whisper_error = model, "listo", None
            try:  # no bloquea el uso: si falla, queda anotado y se verá al intentar transcribir
                check_voice_pipeline()
                _whisper_warning = None
                log.info("Modelo de voz listo (prueba de audio superada)")
            except Exception as e:
                _whisper_warning = f"{type(e).__name__}: {str(e)[:200]}"
                log.warning("Modelo de voz cargado, pero la prueba de audio ha fallado: %s", _whisper_warning)
    return _whisper


def explain_voice_error(e: Exception) -> str:
    """Traduce un fallo de la transcripción a algo que se pueda entender y arreglar."""
    if isinstance(e, ImportError):
        mod = getattr(e, "name", None) or "desconocido"
        if mod.split(".")[0] == "faster_whisper":
            return (
                "Falta la librería de voz (faster-whisper) en el contenedor. "
                "Reconstruye la imagen con: docker compose up -d --build"
            )
        return (
            f"A la librería de voz le falta el módulo de Python «{mod}». "
            "Añádelo a requirements.txt y reconstruye con: docker compose up -d --build"
        )
    name, txt = type(e).__name__, str(e)
    if isinstance(e, TypeError) and "unexpected keyword argument" in txt:
        return (
            f"Incompatibilidad de versiones entre las librerías de voz ({txt[:140]}). "
            "Hay que fijar versiones en requirements.txt."
        )
    red = ("connect", "resolve", "network", "offline", "timed out", "timeout", "name or service", "hub", "internet")
    if any(w in f"{name} {txt}".lower() for w in red):
        return (
            "No he podido descargar el modelo de voz: el servidor parece no tener conexión a internet "
            "(solo hace falta la primera vez). Usa /voz para comprobarlo."
        )
    return f"Error al transcribir ({name}: {txt[:150]}). Detalles: docker logs inventario-bot"


async def load_whisper_bg():
    try:
        await asyncio.to_thread(get_whisper)
    except Exception as e:
        log.warning("No se pudo cargar el modelo de voz: %s", e)


@authorized
async def voz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Estado del reconocimiento de voz; si falló o no se ha cargado, lo reintenta en segundo plano."""
    state = _whisper_state
    icon = {"listo": "✅", "cargando": "⏳", "error": "❌"}.get(state, "💤")
    lines = ["🎙️ Reconocimiento de voz", f"Modelo: {WHISPER_MODEL} · idioma: {WHISPER_LANGUAGE}", f"Estado: {icon} {state}",
             f"Código del bot: {CODE_ID}", f"Librerías: {lib_versions()}"]
    if state == "cargando":
        lines.append("Se está cargando (la primera vez descarga unos 500 MB, puede tardar varios minutos).")
    if state == "listo" and _whisper_warning:
        lines.append(f"⚠️ La prueba de audio falló: {_whisper_warning}")
    if state == "error" and _whisper_error is not None:
        lines.append(f"Causa: {explain_voice_error(_whisper_error)}")
    if state in ("sin cargar", "error"):
        try:
            async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
                r = await client.get("https://huggingface.co")
            lines.append(f"Conexión con Hugging Face (de donde se descarga el modelo): ✅ ({r.status_code})")
        except Exception as e:
            lines.append(f"Conexión con Hugging Face (de donde se descarga el modelo): ❌ {type(e).__name__}")
        task = asyncio.create_task(load_whisper_bg())
        bg = ctx.bot_data.setdefault("bg", set())
        bg.add(task)
        task.add_done_callback(bg.discard)
        lines.append("🔄 Reintento la carga en segundo plano. Vuelve a ejecutar /voz en un minuto.")
    await update.message.reply_text("\n".join(lines))


def transcribe(path: str) -> str:
    model = get_whisper()
    segments, _ = model.transcribe(path, language=WHISPER_LANGUAGE, beam_size=3, vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


@authorized
async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    media = m.voice or m.audio
    if media.duration and media.duration > 120:
        await m.reply_text("⏱️ El audio es demasiado largo (máximo 2 minutos).")
        return
    status = await m.reply_text("🎤 Transcribiendo...")
    if _whisper_state == "cargando":
        await status.edit_text(
            "🎤 El modelo de voz aún se está descargando/cargando (la primera vez puede tardar varios minutos). "
            "Espero a que termine..."
        )
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        tg_file = await media.get_file()
        await tg_file.download_to_drive(path)
        text = await asyncio.to_thread(transcribe, path)
    except Exception as e:
        log.exception("Error transcribiendo el audio")
        await status.edit_text(f"⚠️ {explain_voice_error(e)}")
        return
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if not text:
        await status.edit_text("🤷 No he oído nada, ¿puedes repetirlo?")
        return
    pending, expired = get_pending(ctx)
    if pending and pending["kind"] == "pf":  # la nota de voz es la corrección
        err = await finish_prop_edit(ctx, pending, text, m)
        if err:
            renew_pending(ctx, pending)
            await status.edit_text(f"🎤 «{text}»\n\n🤷 {err}")
        else:
            ctx.user_data.pop("pending", None)
            await status.edit_text(f"🎤 «{text}» ✔️")
        return
    res, err = parse_add_phrase(text)
    if res:
        await propose_add(m, ctx, res, text, "🎤", replace=status)
    else:
        await status.edit_text(
            f"🎤 «{text}»\n\n"
            + (
                err
                or (
                    EXPIRED_NOTE
                    if expired
                    else f"🤷 Por ahora solo entiendo órdenes para añadir objetos. Di, por ejemplo: {EXAMPLE}."
                )
            )
        )


# Lista de comandos que Telegram sugiere al escribir «/» (en orden de uso habitual).
# Se registra sola al arrancar, así nunca se queda desfasada respecto al código.
BOT_COMMANDS = [
    ("add", "Añadir un objeto (nombre, cantidad, lugar, categoría…)"),
    ("busca", "Buscar por nombre, tag o nota"),
    ("lista", "Ver todo el inventario"),
    ("lugares", "Ver los lugares y su contenido"),
    ("categorias", "Ver las categorías"),
    ("tags", "Ver y eliminar tags"),
    ("lugar", "Qué hay en un lugar (escribe parte del nombre)"),
    ("categoria", "Qué hay en una categoría"),
    ("tag", "Objetos que tienen un tag"),
    ("borrartag", "Eliminar tags de todos los objetos"),
    ("bloqueados", "Tags que la IA ya no genera"),
    ("retag", "Generar tags con IA a los que no tienen"),
    ("cantidad", "Fijar la cantidad de un objeto (por id)"),
    ("mover", "Cambiar de lugar un objeto (por id)"),
    ("setcat", "Cambiar la categoría de un objeto (por id)"),
    ("settags", "Cambiar los tags de un objeto (por id)"),
    ("borrar", "Eliminar un objeto (por id)"),
    ("voz", "Estado del reconocimiento de voz"),
    ("cancelar", "Cancelar una edición a medias"),
    ("help", "Ayuda y ejemplos"),
]


async def post_init(app):
    """Al arrancar, registra los comandos en Telegram y precarga el modelo de voz en segundo plano."""
    try:
        await app.bot.set_my_commands(BOT_COMMANDS)
        log.info("Comandos registrados en Telegram (%d)", len(BOT_COMMANDS))
    except Exception as e:  # no es crítico: el bot funciona igual, solo faltaría la sugerencia al escribir «/»
        log.warning("No se pudieron registrar los comandos en Telegram: %s", e)

    async def preload():
        try:
            await asyncio.to_thread(get_whisper)
        except Exception as e:
            log.warning("No se pudo precargar el modelo de voz (se reintentará con el primer audio): %s", e)

    app.create_task(preload())


# ---------- Botones ----------
PICKERS = {
    "mv": dict(col="location", prompt="📍 ¿A dónde lo mueves?", pick="mvp", new="mvn"),
    "ct": dict(col="category", prompt="🏷️ ¿Qué categoría le pones?", pick="ctp", new="ctn"),
}


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if ALLOWED and q.from_user.id not in ALLOWED:
        await q.answer("⛔ No tienes permiso.", show_alert=True)
        return
    act, *a = q.data.split(":")
    t0 = time.monotonic()
    try:
        await dispatch(q, ctx, act, a)
    except BadRequest as e:
        if "not modified" not in str(e).lower():  # pulsar algo que no cambia nada no es un error
            log.warning("Error en botón %s: %s", q.data, e)
    except Exception:
        # Cualquier otro fallo: se deja el detalle en el log y se avisa en pantalla, en vez de dejar el
        # botón «pensando» sin respuesta.
        log.exception("Error inesperado en el botón %s", q.data)
        try:
            await q.answer("⚠️ Algo ha fallado. Inténtalo de nuevo (el detalle queda en el log del bot).", show_alert=True)
        except Exception:
            pass  # ya estaba respondido, o Telegram no responde
    finally:
        dt = time.monotonic() - t0
        if dt > 3:
            log.warning("El botón %s tardó %.1f s en procesarse (¿servidor saturado?)", q.data, dt)


async def dispatch(q, ctx, act: str, a: list[str]):
    msg = q.message

    if act == "noop":
        await q.answer()
        return

    if act == "pg":  # cambiar de página
        content = view_content(ctx, a[0], int(a[1]))
        if not content:
            await q.answer("Esta lista ya no está disponible. Repite la búsqueda.", show_alert=True)
            return
        await q.answer()
        await q.edit_message_text(content[0], reply_markup=content[1])
        return

    if act in ("pl", "ca"):  # ver contenido de un lugar / categoría
        col, icon = ("location", "📍") if act == "pl" else ("category", "🏷️")
        rep = get_item(int(a[0]))
        if not rep:
            await q.answer("Ese elemento ya no existe. Repite /lugares o /categorias.", show_alert=True)
            return
        await q.answer()
        ids = query_ids(f"{col}_norm=?", (rep[f"{col}_norm"],), "name_norm")
        await show_view(msg, ctx, ids, f"{icon} {rep[col]}", "🤷 No hay nada.")
        return

    if act in ("vy", "vn", "ve", "vb", "vf", "vp", "vw"):  # objeto dictado: confirmar / editar
        tok = a[0]
        props = ctx.bot_data.get("proposals", {})
        prop = props.get(tok)
        if not prop:
            await q.answer("Esto ya se procesó o caducó. Vuelve a dictarlo.", show_alert=True)
            return
        await q.answer()
        if act == "vn":
            props.pop(tok, None)
            await q.edit_message_text("👍 Cancelado.")
        elif act == "vy":
            props.pop(tok, None)  # al sacarlo, una segunda pulsación no lo duplica
            await q.edit_message_text(f"✅ Confirmado: {prop['name']}")
            await save_item(msg, prop["name"], prop["qty"], prop["location"], prop["category"], prop.get("tags"), prop.get("note", ""))
        elif act == "ve":  # menú de edición
            text, kb = edit_menu_content(tok, prop)
            await q.edit_message_text(text, reply_markup=kb)
        elif act == "vb":  # volver al resumen
            text, kb = proposal_content(tok, prop)
            await q.edit_message_text(text, reply_markup=kb)
        elif act == "vf" and a[1] in ("l", "c"):  # lugar / categoría: primero los que ya existen
            col = "location" if a[1] == "l" else "category"
            with db() as c:
                rows = c.execute(
                    f"SELECT {col} AS label, MIN(id) AS rep, COUNT(*) AS n FROM items "
                    f"GROUP BY {col}_norm ORDER BY n DESC, {col}_norm LIMIT 12"
                ).fetchall()
            if not rows:
                await ask_prop_value(ctx, msg, tok, a[1])
            else:
                opts = [btn(r["label"][:28], f"vp:{tok}:{a[1]}:{r['rep']}") for r in rows]
                kb = [opts[i : i + 2] for i in range(0, len(opts), 2)]
                kb.append([btn("✏️ Escribir o dictar otro...", f"vw:{tok}:{a[1]}")])
                kb.append([btn("↩️ Volver", f"ve:{tok}")])
                prompt = "📍 ¿Dónde lo guardo?" if a[1] == "l" else "🏷️ ¿Qué categoría le pongo?"
                await q.edit_message_text(f"{prompt}\n«{prop['name']}»", reply_markup=InlineKeyboardMarkup(kb))
        elif act == "vp":  # elegida una opción existente
            rep = get_item(int(a[2]))
            if not rep:
                await msg.reply_text("Esa opción ya no existe, inténtalo de nuevo.")
                return
            col = "location" if a[1] == "l" else "category"
            prop[col] = rep[col]
            prop["edited"] = True
            text, kb = proposal_content(tok, prop)
            await q.edit_message_text(text, reply_markup=kb)
        else:  # vf (nombre / cantidad) o vw (escribir otro lugar / categoría)
            await ask_prop_value(ctx, msg, tok, a[1])
        return

    if act == "tp":  # página de la lista de tags / cambiar orden
        content = tags_page_content(ctx, int(a[1]), a[0])
        await q.answer()
        if not content:
            await q.edit_message_text("Todavía no hay tags.")
        else:
            await q.edit_message_text(content[0], reply_markup=content[1])
        return

    if act in ("ts", "tv", "td", "tdy", "tc", "ub"):  # acciones sobre tags
        tok = a[-1]
        pairs = lookup_tags(ctx, tok)
        if not pairs:
            await q.answer("Esa lista de tags ya no está disponible. Vuelve a abrir /tags.", show_alert=True)
            return
        norms = {n for n, _ in pairs}
        names = ", ".join(f"«{l}»" for _, l in pairs)
        if act == "ts":  # menú de un tag
            n_items = count_items_with(norms)
            if n_items == 0:
                await q.answer("Ese tag ya no existe.", show_alert=True)
                return
            await q.answer()
            kb = InlineKeyboardMarkup([[btn("👁 Ver objetos", f"tv:{tok}"), btn("🗑️ Eliminar", f"td:{tok}")]])
            await msg.reply_text(f"🔖 {names} — {n_items} objeto(s)", reply_markup=kb)
        elif act == "tv":  # ver los objetos con ese tag
            await q.answer()
            ids = ids_with_tag(pairs[0][0])
            await show_view(msg, ctx, ids, f"🔖 {pairs[0][1]}", "🤷 Ya no hay objetos con ese tag.")
        elif act == "td":  # pedir confirmación
            await q.answer()
            text, kb = delete_confirm(pairs, tok, count_items_with(norms))
            await q.edit_message_text(text, reply_markup=kb)
        elif act == "tdy":  # eliminar (a[0] == "1": además, bloquear)
            changed = remove_tags(norms)
            if a[0] == "1":
                block_tags(pairs)
            await q.answer("Eliminado")
            extra = " La IA no volverá a proponerlos." if a[0] == "1" else ""
            await q.edit_message_text(f"✅ {names} eliminado(s) de {changed} objeto(s).{extra}")
        elif act == "tc":
            await q.answer()
            await q.edit_message_text("👍 Cancelado.")
        else:  # ub: permitir de nuevo un tag bloqueado
            unblock_tags(norms)
            await q.answer("Permitido de nuevo")
            text, kb = blocked_content(ctx)
            await q.edit_message_text(text, reply_markup=kb)
        return

    item_id = int(a[0])

    if act == "it":  # abrir ficha (en mensaje nuevo, para no perder la lista)
        await q.answer()
        await send_card(msg, item_id)

    elif act == "bk":  # volver a la ficha
        await q.answer()
        await refresh_card(q, item_id)

    elif act == "q":  # +1 / -1
        with db() as c:
            c.execute("UPDATE items SET quantity=MAX(0, quantity+?) WHERE id=?", (int(a[1]), item_id))
        await q.answer()
        await refresh_card(q, item_id)

    elif act == "qs":  # escribir cantidad exacta
        set_pending(ctx, kind="qty", id=item_id)
        await q.answer()
        await msg.reply_text("✏️ Escribe la nueva cantidad (o /cancelar):")

    elif act in PICKERS:  # menú de lugares / categorías existentes
        cfg, r = PICKERS[act], get_item(item_id)
        if not r:
            await q.answer("Ese objeto ya no existe.", show_alert=True)
            return
        col = cfg["col"]
        with db() as c:
            rows = c.execute(
                f"SELECT {col} AS label, MIN(id) AS rep, COUNT(*) AS n FROM items "
                f"GROUP BY {col}_norm ORDER BY n DESC, {col}_norm LIMIT 12"
            ).fetchall()
        opts = [btn(x["label"][:28], f"{cfg['pick']}:{item_id}:{x['rep']}") for x in rows]
        kb = [opts[i : i + 2] for i in range(0, len(opts), 2)]
        kb.append([btn("✏️ Otro...", f"{cfg['new']}:{item_id}")])
        kb.append([btn("↩️ Volver", f"bk:{item_id}")])
        await q.answer()
        await q.edit_message_text(f"{cfg['prompt']}\n«{r['name']}»", reply_markup=InlineKeyboardMarkup(kb))

    elif act in ("mvp", "ctp"):  # elegida una opción existente
        col = PICKERS["mv" if act == "mvp" else "ct"]["col"]
        rep = get_item(int(a[1]))
        if not rep:
            await q.answer("Esa opción ya no existe, inténtalo de nuevo.", show_alert=True)
            return
        with db() as c:
            c.execute(
                f"UPDATE items SET {col}=?, {col}_norm=? WHERE id=?",
                (rep[col], rep[f"{col}_norm"], item_id),
            )
        await q.answer("Hecho")
        await refresh_card(q, item_id)

    elif act in ("mvn", "ctn"):  # escribir uno nuevo
        kind = "loc" if act == "mvn" else "cat"
        set_pending(ctx, kind=kind, id=item_id)
        what = "el nuevo lugar" if kind == "loc" else "la nueva categoría"
        await q.answer()
        await msg.reply_text(f"✏️ Escribe {what} (o /cancelar):")

    elif act == "te":  # escribir tags a mano
        set_pending(ctx, kind="tags", id=item_id)
        await q.answer()
        await msg.reply_text("✏️ Escribe los tags separados por comas (o /cancelar):")

    elif act == "nt":  # escribir la nota / descripción
        set_pending(ctx, kind="note", id=item_id)
        await q.answer()
        await msg.reply_text("✏️ Escribe la nota o descripción (o «ninguna» para quitarla; /cancelar para salir):")

    elif act == "tg":  # regenerar tags con IA
        r = get_item(item_id)
        if not r:
            await q.answer("Ese objeto ya no existe.", show_alert=True)
            return
        await q.answer("Generando tags...")
        await q.edit_message_text(card_text(r, note="⏳ Generando tags..."))
        tags = await generate_tags(r["name"], r["category"], r["description"])
        if tags:
            with db() as c:
                save_tags(c, item_id, tags)
            note = "✅ Tags actualizados"
        elif tags is None:
            note = "⚠️ La IA no está disponible"
        else:
            note = "La IA no propuso tags"
        await refresh_card(q, item_id, note)

    elif act == "dl":  # confirmar borrado
        r = get_item(item_id)
        if not r:
            await q.answer("Ese objeto ya no existe.", show_alert=True)
            return
        kb = InlineKeyboardMarkup(
            [[btn("🗑️ Sí, borrar", f"dly:{item_id}"), btn("Cancelar", f"bk:{item_id}")]]
        )
        await q.answer()
        await q.edit_message_text(f"¿Borrar «{r['name']}» (×{r['quantity']}, {r['location']})?", reply_markup=kb)

    elif act == "dly":  # borrado confirmado
        r = get_item(item_id)
        with db() as c:
            c.execute("DELETE FROM items WHERE id=?", (item_id,))
        await q.answer("Eliminado")
        await q.edit_message_text(f"🗑️ «{r['name']}» eliminado." if r else "🗑️ Eliminado.")

    else:
        await q.answer()


@authorized
async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Texto suelto: si hay una edición pendiente (tras pulsar un botón), se aplica."""
    p, expired = get_pending(ctx, consume=True)
    if not p:
        text = update.message.text
        res, err = parse_add_phrase(text)  # ¿es algo como «añade tornillos en el cajón 2»?
        if res:
            await propose_add(update.message, ctx, res, text, "💬")
        elif err:
            await update.message.reply_text(err)
        elif expired:
            await update.message.reply_text(EXPIRED_NOTE)
        else:
            await update.message.reply_text("🤔 No te he entendido. Prueba /busca texto, o /help para ver los comandos.")
        return
    if p["kind"] == "pf":  # corrección de un objeto pendiente de confirmar
        err = await finish_prop_edit(ctx, p, update.message.text, update.message)
        if err:
            renew_pending(ctx, p)  # seguimos esperando una respuesta válida
            await update.message.reply_text(f"{err} (o /cancelar)")
        return
    value, item_id, kind = update.message.text.strip(), p["id"], p["kind"]
    row = get_item(item_id)
    if not row:
        await update.message.reply_text("🤷 Ese objeto ya no existe.")
        return
    if not value:
        renew_pending(ctx, p)
        await update.message.reply_text("Escribe algo (o /cancelar).")
        return

    if kind == "qty":
        if not value.isdigit():
            renew_pending(ctx, p)  # seguimos esperando una respuesta válida
            await update.message.reply_text("Escribe un número entero (o /cancelar).")
            return
        sql, params = "UPDATE items SET quantity=? WHERE id=?", (int(value), item_id)
    elif kind == "loc":
        sql, params = "UPDATE items SET location=?, location_norm=? WHERE id=?", (value, norm(value), item_id)
    elif kind == "cat":
        sql, params = "UPDATE items SET category=?, category_norm=? WHERE id=?", (value, norm(value), item_id)
    elif kind == "note":
        new = "" if norm(value) in ("ninguna", "ninguno") else re.sub(
            r"^\s*(?:notas?|descripci[oó]n)\s*:\s*", "", value, flags=re.I
        )
        sql, params = "UPDATE items SET description=?, description_norm=? WHERE id=?", (new, norm(new), item_id)
    else:  # tags
        apply_manual_tags(row, value.split(","))
        await send_card(update.message, item_id, note="✅ Tags actualizados")
        return

    with db() as c:
        c.execute(sql, params)
    await send_card(update.message, item_id, note="✅ Actualizado")


# ---------- Comandos avanzados (por id) ----------
@authorized
async def cantidad(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        item_id, qty = int(ctx.args[0]), int(ctx.args[1])
        if qty < 0:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /cantidad id n")
        return
    with db() as c:
        cur = c.execute("UPDATE items SET quantity=? WHERE id=?", (qty, item_id))
    await update.message.reply_text(
        f"✏️ #{item_id} ahora tiene cantidad {qty}." if cur.rowcount else "No existe ese id."
    )


@authorized
async def mover(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        id_part, place = split_args(args_text(ctx), 1)
        item_id, place = int(id_part), place.strip()
        if not place:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Uso: /mover id, nuevo lugar")
        return
    with db() as c:
        cur = c.execute(
            "UPDATE items SET location=?, location_norm=? WHERE id=?",
            (place, norm(place), item_id),
        )
    await update.message.reply_text(
        f"🚚 #{item_id} movido a {place}." if cur.rowcount else "No existe ese id."
    )


@authorized
async def setcat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        id_part, cat = split_args(args_text(ctx), 1)
        item_id, cat = int(id_part), cat.strip()
        if not cat:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Uso: /setcat id, nueva categoría")
        return
    with db() as c:
        cur = c.execute(
            "UPDATE items SET category=?, category_norm=? WHERE id=?",
            (cat, norm(cat), item_id),
        )
    await update.message.reply_text(
        f"🏷️ #{item_id} ahora está en la categoría {cat}." if cur.rowcount else "No existe ese id."
    )


@authorized
async def settags(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        id_part, tags_part = split_args(args_text(ctx), 1)
        item_id = int(id_part)
    except ValueError:
        await update.message.reply_text("Uso: /settags id, tag1, tag2, tag3")
        return
    row = get_item(item_id)
    if not row:
        await update.message.reply_text("No existe ese id.")
        return
    tags = apply_manual_tags(row, tags_part.split(","))
    await update.message.reply_text(f"🔖 #{item_id}: {', '.join(tags) or '(sin tags)'}")


@authorized
async def retag(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    arg = args_text(ctx).lower()
    with db() as c:
        if arg == "todos":
            rows = c.execute("SELECT * FROM items WHERE tags=''").fetchall()
        elif arg.isdigit():
            rows = c.execute("SELECT * FROM items WHERE id=?", (int(arg),)).fetchall()
        else:
            rows = None
    if rows is None:
        await update.message.reply_text("Uso: /retag todos   (para uno solo, usa el botón «Tags con IA» de su ficha)")
        return
    if not rows:
        await update.message.reply_text("No hay nada que etiquetar.")
        return

    msg = await update.message.reply_text(f"⏳ Generando tags para {len(rows)} objeto(s)...")
    done = 0
    for r in rows:
        tags = await generate_tags(r["name"], r["category"], r["description"])
        if tags is None:
            await msg.edit_text(f"⚠️ La IA no está disponible. Etiquetados: {done}/{len(rows)}.")
            return
        if tags:
            with db() as c:
                save_tags(c, r["id"], tags)
            done += 1
        if len(rows) == 1:
            await msg.edit_text(f"🔖 {r['name']}: {', '.join(tags) or '(sin tags)'}")
            return
    await msg.edit_text(f"✅ Tags generados para {done}/{len(rows)} objetos.")


@authorized
async def borrar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        item_id = int(ctx.args[0])
    except (IndexError, ValueError):
        await update.message.reply_text("Uso: /borrar id")
        return
    with db() as c:
        cur = c.execute("DELETE FROM items WHERE id=?", (item_id,))
    await update.message.reply_text(
        f"🗑️ #{item_id} eliminado." if cur.rowcount else "No existe ese id."
    )


async def on_error(update, ctx: ContextTypes.DEFAULT_TYPE):
    """Cualquier error no controlado queda en el log con su detalle, y el usuario recibe un aviso."""
    log.error("Error no controlado: %s", ctx.error, exc_info=ctx.error)
    try:
        cq = getattr(update, "callback_query", None)
        msg = getattr(update, "effective_message", None)
        if cq is not None:
            await cq.answer("⚠️ Error interno. Inténtalo de nuevo (detalle en el log del bot).", show_alert=True)
        elif msg is not None:
            await msg.reply_text("⚠️ Ha ocurrido un error interno. Inténtalo de nuevo; el detalle queda en el log del bot.")
    except Exception:
        pass


def main():
    init_db()
    # concurrent_updates: mientras la IA piensa, el bot sigue atendiendo otros mensajes
    builder = Application.builder().token(TOKEN).concurrent_updates(True).post_init(post_init)
    # Por defecto la librería espera solo 5 s a Telegram; con la IA o la voz usando la CPU, eso es poco.
    for method, seconds in (("connect_timeout", 10), ("read_timeout", 20), ("write_timeout", 20), ("pool_timeout", 10)):
        if hasattr(builder, method):
            builder = getattr(builder, method)(seconds)
    app = builder.build()
    app.add_error_handler(on_error)
    for name, fn in [
        ("start", start), ("help", start), ("cancelar", cancelar), ("add", add),
        ("busca", buscar), ("lugar", lugar), ("lugares", lugares),
        ("categoria", categoria), ("categorias", categorias), ("tag", tag),
        ("tags", tags_list), ("borrartag", borrartag), ("bloqueados", bloqueados),
        ("lista", lista), ("cantidad", cantidad),
        ("mover", mover), ("setcat", setcat), ("settags", settags),
        ("retag", retag), ("borrar", borrar), ("voz", voz),
    ]:
        app.add_handler(CommandHandler(name, fn))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    log.info("Código %s · librerías de voz: %s", CODE_ID, lib_versions())
    log.info("Bot en marcha (IA: %s · voz: %s)", OLLAMA_MODEL, WHISPER_MODEL)
    app.run_polling()


if __name__ == "__main__":
    main()
