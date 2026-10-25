import logging
import os
import sqlite3
import unicodedata
from functools import wraps

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()}
DB_PATH = os.environ.get("DB_PATH", "/data/inventario.db")
DEFAULT_CAT = "Sin categoría"

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
        # Migración: añade la categoría si la base de datos es de una versión anterior
        cols = {r["name"] for r in c.execute("PRAGMA table_info(items)")}
        if "category" not in cols:
            log.info("Migrando base de datos: añadiendo columnas de categoría")
            c.execute(f"ALTER TABLE items ADD COLUMN category TEXT NOT NULL DEFAULT '{DEFAULT_CAT}'")
            c.execute(f"ALTER TABLE items ADD COLUMN category_norm TEXT NOT NULL DEFAULT '{norm(DEFAULT_CAT)}'")


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


def fmt(rows) -> str:
    return "\n".join(
        f"#{r['id']} · {r['name']} ×{r['quantity']} → 📍 {r['location']} · 🏷️ {r['category']}"
        for r in rows
    )


async def reply_long(update: Update, text: str):
    for i in range(0, len(text), 4000):
        await update.message.reply_text(text[i : i + 4000])


def args_text(ctx) -> str:
    return " ".join(ctx.args).strip()


# ---------- Comandos ----------
HELP = (
    "📦 *Inventario del estudio*\n\n"
    "/add nombre | cantidad | lugar | categoría\n"
    "   ej: `/add Destornillador Phillips | 3 | Cajón 2 | Herramientas`\n"
    "   Cantidad y categoría son opcionales:\n"
    "   `/add Taladro | Armario`\n"
    "   `/add Taladro | Armario | Herramientas`\n"
    "/buscar texto – dónde está algo\n"
    "/lugar texto – qué hay en un lugar\n"
    "/categoria texto – qué hay en una categoría\n"
    "/lugares – lugares y nº de objetos\n"
    "/categorias – categorías y nº de objetos\n"
    "/lista – todo el inventario\n"
    "/cantidad id n – fijar cantidad\n"
    "/mover id | nuevo lugar\n"
    "/setcat id | nueva categoría\n"
    "/borrar id"
)


@authorized
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode="Markdown")


@authorized
async def add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = [p.strip() for p in args_text(ctx).split("|")]
    try:
        name = parts[0]
        if len(parts) > 1 and parts[1].isdigit():
            # nombre | cantidad | lugar [| categoría]
            qty, place = int(parts[1]), parts[2]
            cat = parts[3] if len(parts) > 3 else ""
        else:
            # nombre | lugar [| categoría]
            qty, place = 1, parts[1]
            cat = parts[2] if len(parts) > 2 else ""
        if not name or not place or qty < 1:
            raise ValueError
    except (IndexError, ValueError):
        await update.message.reply_text(
            "Formato: /add nombre | cantidad | lugar | categoría\n"
            "Ej: /add Cinta métrica | 2 | Cajón 1 | Herramientas"
        )
        return

    with db() as c:
        row = c.execute(
            "SELECT * FROM items WHERE name_norm=? AND location_norm=?",
            (norm(name), norm(place)),
        ).fetchone()
        if row:  # mismo objeto en el mismo sitio: sumamos
            c.execute("UPDATE items SET quantity=quantity+? WHERE id=?", (qty, row["id"]))
            if cat:  # si se indicó categoría, la actualizamos
                c.execute(
                    "UPDATE items SET category=?, category_norm=? WHERE id=?",
                    (cat, norm(cat), row["id"]),
                )
            await update.message.reply_text(
                f"➕ Ya existía, ahora hay {row['quantity'] + qty} de «{row['name']}» "
                f"en {row['location']} (#{row['id']})."
            )
        else:
            cat = cat or DEFAULT_CAT
            cur = c.execute(
                """INSERT INTO items
                   (name, name_norm, quantity, location, location_norm, category, category_norm)
                   VALUES (?,?,?,?,?,?,?)""",
                (name, norm(name), qty, place, norm(place), cat, norm(cat)),
            )
            await update.message.reply_text(
                f"✅ Guardado #{cur.lastrowid}: {name} ×{qty} → 📍 {place} · 🏷️ {cat}"
            )


@authorized
async def buscar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = norm(args_text(ctx))
    if not q:
        await update.message.reply_text("Uso: /buscar texto")
        return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE name_norm LIKE ? ORDER BY name_norm",
            (f"%{q}%",),
        ).fetchall()
    await reply_long(update, fmt(rows) if rows else "🤷 No encuentro nada con ese nombre.")


@authorized
async def lugar(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = norm(args_text(ctx))
    if not q:
        await update.message.reply_text("Uso: /lugar texto")
        return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE location_norm LIKE ? ORDER BY location_norm, name_norm",
            (f"%{q}%",),
        ).fetchall()
    await reply_long(update, fmt(rows) if rows else "🤷 No hay nada en ese lugar.")


@authorized
async def categoria(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = norm(args_text(ctx))
    if not q:
        await update.message.reply_text("Uso: /categoria texto\n(mira /categorias para ver las que existen)")
        return
    with db() as c:
        rows = c.execute(
            "SELECT * FROM items WHERE category_norm LIKE ? ORDER BY category_norm, name_norm",
            (f"%{q}%",),
        ).fetchall()
    await reply_long(update, fmt(rows) if rows else "🤷 No hay nada en esa categoría.")


@authorized
async def lugares(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute(
            "SELECT location, COUNT(*) n FROM items GROUP BY location_norm ORDER BY location_norm"
        ).fetchall()
    text = "\n".join(f"📍 {r['location']} ({r['n']})" for r in rows) or "Inventario vacío."
    await reply_long(update, text)


@authorized
async def categorias(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute(
            "SELECT category, COUNT(*) n FROM items GROUP BY category_norm ORDER BY category_norm"
        ).fetchall()
    text = "\n".join(f"🏷️ {r['category']} ({r['n']})" for r in rows) or "Inventario vacío."
    await reply_long(update, text)


@authorized
async def lista(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    with db() as c:
        rows = c.execute("SELECT * FROM items ORDER BY category_norm, name_norm").fetchall()
    await reply_long(update, fmt(rows) if rows else "Inventario vacío.")


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
        id_part, place = args_text(ctx).split("|", 1)
        item_id, place = int(id_part), place.strip()
        if not place:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Uso: /mover id | nuevo lugar")
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
        id_part, cat = args_text(ctx).split("|", 1)
        item_id, cat = int(id_part), cat.strip()
        if not cat:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Uso: /setcat id | nueva categoría")
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


def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    for name, fn in [
        ("start", start), ("help", start), ("add", add), ("buscar", buscar),
        ("lugar", lugar), ("lugares", lugares), ("categoria", categoria),
        ("categorias", categorias), ("lista", lista), ("cantidad", cantidad),
        ("mover", mover), ("setcat", setcat), ("borrar", borrar),
    ]:
        app.add_handler(CommandHandler(name, fn))
    log.info("Bot en marcha")
    app.run_polling()


if __name__ == "__main__":
    main()
