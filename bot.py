"""
Бот-автопостер для канала «Идея в кассу» в мессенджере MAX.

Что делает:
  1. За PREP_MINUTES до каждого слота публикации берёт свежие новости из RSS,
     просит GigaChat написать пост по рубрике и подбирает фото на Pixabay.
  2. Отправляет черновик всем админам в личку с кнопками модерации.
  3. После одобрения публикует пост в канал в своё время (или сразу).

Запуск:            python bot.py
Узнать ID канала:  опубликуйте пост в канале — бот пришлёт ID админам
Тестовый пост:     python bot.py test   (сгенерирует и пришлёт черновик админам)
"""

import os
import re
import sys
import time
import uuid
import random
import sqlite3
import logging
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import feedparser
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vkasu")

# ─────────────────────────── Настройки ───────────────────────────

def env(name, default=None, required=False):
    val = (os.getenv(name) or "").strip() or default  # пустая переменная = не задана
    if required and not val:
        sys.exit(f"Не задана переменная {name} в файле .env")
    return val

MAX_API = env("MAX_API_URL", "https://platform-api2.max.ru").rstrip("/")
MAX_TOKEN = env("MAX_TOKEN", required=True)
_ch = env("CHANNEL_ID", "0")
if not re.fullmatch(r"-?\d+", _ch):
    sys.exit(f"CHANNEL_ID должен быть числом, например -123456789, а сейчас: {_ch!r}")
CHANNEL_ID = int(_ch)
_bad = [x for x in re.split(r"[,\s]+", env("ADMIN_IDS", "")) if x and not x.isdigit()]
if _bad:
    sys.exit(f"ADMIN_IDS — только числа через запятую. Непонятно: {', '.join(_bad)}")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", env("ADMIN_IDS", "")) if x}

GIGACHAT_KEY = re.sub(r"^\s*(Basic\s+)?", "", env("GIGACHAT_AUTH_KEY", "")).strip() or None
GIGACHAT_SCOPE = env("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
GIGACHAT_MODEL = env("GIGACHAT_MODEL", "GigaChat-2-Pro")

PIXABAY_KEY = env("PIXABAY_API_KEY")   # pixabay.com/api/docs — бесплатно
PEXELS_KEY = env("PEXELS_API_KEY")     # необязательно, если есть ключ

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Папка data/ на хостингах обычно сохраняется между перезапусками
DATA_DIR = os.path.join(BASE_DIR, "data") if os.path.isdir(os.path.join(BASE_DIR, "data")) else BASE_DIR


def build_ca_bundle():
    """Сертификат Минцифры (Russian Trusted Root CA) нужен для GigaChat и API MAX.

    Достаточно положить файл(ы) сертификата рядом с bot.py или в data/ — с любым
    именем, содержащим «russian» (.cer, .crt или .pem, текстовый или двоичный).
    Бот сам склеит их со стандартными сертификатами. CA_BUNDLE в .env — для ручного пути.
    """
    explicit = os.getenv("CA_BUNDLE")
    if explicit:
        return explicit
    found = []
    for folder in {BASE_DIR, DATA_DIR}:
        for name in sorted(os.listdir(folder)):
            if re.search(r"russian.*\.(cer|crt|pem)$", name, re.I):
                found.append(os.path.join(folder, name))
    if not found:
        log.warning("Сертификат Минцифры не найден. Файлы в %s: %s", BASE_DIR,
                    ", ".join(sorted(os.listdir(BASE_DIR))))
        return True
    import ssl
    import certifi
    with open(certifi.where(), encoding="utf-8") as f:
        bundle = f.read()
    for path in found:
        with open(path, "rb") as f:
            raw = f.read()
        if b"BEGIN CERTIFICATE" not in raw and not raw.startswith(b"\x30"):
            # файл мог быть сохранён хостингом в base64 — пробуем расшифровать
            import base64
            try:
                raw = base64.b64decode(b"".join(raw.split()), validate=True)
            except Exception:  # noqa: BLE001
                log.warning("Не удалось прочитать сертификат %s", path)
                continue
        pem = raw.decode("utf-8", "ignore") if b"BEGIN CERTIFICATE" in raw else ssl.DER_cert_to_PEM_cert(raw)
        bundle += "\n" + pem.strip() + "\n"
    out = os.path.join(DATA_DIR, "ca_bundle_combined.pem")
    with open(out, "w", encoding="utf-8") as f:
        f.write(bundle)
    logging.getLogger("vkasu").info("Сертификаты Минцифры подключены: %s",
                                    ", ".join(os.path.basename(p) for p in found))
    return out


CA_BUNDLE = build_ca_bundle()

TZ = ZoneInfo(env("TIMEZONE", "Europe/Moscow"))
SLOTS = [s.strip() for s in env("SLOTS", "08:30,11:30,14:30,17:30,20:30").split(",") if s.strip()]
PREP_MINUTES = int(env("PREP_MINUTES", "60"))

RSS_FEEDS = [u.strip() for u in env(
    "RSS_FEEDS",
    "https://rssexport.rbc.ru/rbcnews/news/30/full.rss,https://vc.ru/rss/all",
).split(",") if u.strip()]

DB_PATH = env("DB_PATH", os.path.join(DATA_DIR, "bot.db"))


# ─────────────────────────── Рубрики и стиль ───────────────────────────

RUBRICS = [
    ("💡 Идея дня",
     "одна конкретная бизнес-идея: суть, кому продавать, ориентировочные вложения на старт "
     "(вилкой), где искать первых клиентов и главный риск"),
    ("💸 Без вложений",
     "идея, которую можно начать почти без денег — на своих навыках, времени и телефоне; "
     "дай 3 первых шага, которые можно сделать уже на этой неделе"),
    ("🔍 Ниша под лупой",
     "разбор одной ниши: почему она интересна сейчас, кто клиент, из чего складывается "
     "выручка, 3 плюса и 3 подводных камня"),
    ("🌍 Подсмотрено за рубежом",
     "формат бизнеса, популярный за рубежом и пока редкий в России; как адаптировать его "
     "под наши реалии"),
    ("❌ Разбор ошибок",
     "типичные ошибки, из-за которых закрываются бизнесы в одной нише, и как их избежать"),
]

SYSTEM_PROMPT = """Ты автор популярного канала «Идея в кассу» в мессенджере MAX.
Канал про бизнес-идеи, которые можно запустить в России уже сейчас.
Описание канала: «Идеи для бизнеса, которые можно запустить уже сейчас. Разбираем ниши,
считаем вложения и прибыль. Без воды — только то, что идёт в кассу».

Правила текста:
- Пиши по-русски, живо и просто, как умный друг-предприниматель, без канцелярита и воды.
- Объём 700–1200 знаков.
- Первая строка — цепляющий заголовок жирным: **Заголовок**. Название рубрики в начале не пиши.
- Короткие абзацы, 3–6 уместных эмодзи, можно короткие списки.
- Не выдумывай точные цифры, статистику и названия компаний. Деньги указывай как
  ориентировочную вилку и помечай «примерно» или «ориентировочно».
- Если используешь новость, не пересказывай её — бери только как повод и пиши своими словами.
- Никаких обещаний лёгких денег и «успешного успеха».
- В конце — вопрос к читателям, чтобы они написали в комментариях.
"""

POST_FORMAT = """Ответь строго в таком формате, без пояснений до и после:
ИСТОЧНИК: <номер новости, которую взял как повод, или 0, если ни одна не подошла>
ФОТО: <2–4 слова на английском для поиска фото на фотостоке>
---
<текст поста>"""

# ─────────────────────────── База данных ───────────────────────────

_db_lock = threading.Lock()


def db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_key TEXT UNIQUE,
            slot_time TEXT,
            rubric INTEGER,
            text TEXT,
            photo_query TEXT,
            image_url TEXT,
            source_url TEXT,
            status TEXT,          -- pending / approved / published / deleted / failed
            publish_at TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS seen_news (link TEXT PRIMARY KEY, at TEXT);
        """)


def q(sql, args=(), one=False):
    with _db_lock, db() as c:
        cur = c.execute(sql, args)
        rows = cur.fetchall()
        c.commit()
        if one:
            return rows[0] if rows else None
        return rows


def insert_post(**f):
    with _db_lock, db() as c:
        cur = c.execute(
            "INSERT INTO posts (slot_key, slot_time, rubric, text, photo_query, image_url, "
            "source_url, status, publish_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f["slot_key"], f.get("slot_time"), f["rubric"], f["text"], f.get("photo_query"),
             f.get("image_url"), f.get("source_url"), "pending", None, now().isoformat()),
        )
        c.commit()
        return cur.lastrowid


def get_post(pid):
    return q("SELECT * FROM posts WHERE id=?", (pid,), one=True)


def update_post(pid, **f):
    cols = ", ".join(f"{k}=?" for k in f)
    q(f"UPDATE posts SET {cols} WHERE id=?", (*f.values(), pid))


def now():
    return datetime.now(TZ)

# ─────────────────────────── MAX Bot API ───────────────────────────


class MaxApi:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["Authorization"] = MAX_TOKEN
        self.s.verify = CA_BUNDLE

    def call(self, method, path, params=None, body=None, timeout=40):
        for attempt in range(5):
            try:
                r = self.s.request(method, MAX_API + path, params=params, json=body, timeout=timeout)
            except requests.RequestException as e:
                log.warning("MAX %s: сеть (%s), повтор", path, e)
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 30))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"MAX {path} → {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else {}
        raise RuntimeError(f"MAX {path}: сервер недоступен")

    def send(self, text, chat_id=None, user_id=None, attachments=None):
        params = {"chat_id": chat_id} if chat_id else {"user_id": user_id}
        body = {"text": text[:4000], "format": "markdown"}
        if attachments:
            body["attachments"] = attachments
        return self.call("POST", "/messages", params=params, body=body)

    def answer(self, callback_id, text, attachments):
        """Меняет сообщение, на кнопку которого нажали. attachments=[] убирает вложения."""
        body = {"message": {"text": text[:4000], "format": "markdown", "attachments": attachments}}
        return self.call("POST", "/answers", params={"callback_id": callback_id}, body=body)

    def updates(self, marker):
        params = {"timeout": 25}
        if marker is not None:
            params["marker"] = marker
        return self.call("GET", "/updates", params=params, timeout=40)


mx = MaxApi()


def image_att(url):
    return {"type": "image", "payload": {"url": url}}


def keyboard(rows):
    return {"type": "inline_keyboard", "payload": {"buttons": [
        [{"type": "callback", "text": t, "payload": p} for t, p in row] for row in rows
    ]}}

# ─────────────────────────── GigaChat ───────────────────────────


class GigaChat:
    OAUTH = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    CHAT = "https://gigachat.devices.sberbank.ru/api/v1/chat/completions"

    def __init__(self):
        self.token, self.exp = None, 0

    def _auth(self):
        r = requests.post(self.OAUTH, headers={
            "Authorization": f"Basic {GIGACHAT_KEY}",
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }, data={"scope": GIGACHAT_SCOPE}, verify=CA_BUNDLE, timeout=30)
        r.raise_for_status()
        d = r.json()
        self.token = d["access_token"]
        self.exp = d["expires_at"] / 1000 - 60  # expires_at в миллисекундах

    def chat(self, system, user, temperature=0.85):
        if not GIGACHAT_KEY:
            raise RuntimeError("Не задан GIGACHAT_AUTH_KEY")
        if time.time() > self.exp:
            self._auth()
        r = requests.post(self.CHAT, headers={"Authorization": f"Bearer {self.token}"}, json={
            "model": GIGACHAT_MODEL,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": temperature,
        }, verify=CA_BUNDLE, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()


giga = GigaChat()

# ─────────────────────────── Новости и фото ───────────────────────────


def fresh_news(limit=15):
    items = []
    border = time.time() - 48 * 3600
    seen = {r["link"] for r in q("SELECT link FROM seen_news")}
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as e:  # noqa: BLE001
            log.warning("RSS %s: %s", url, e)
            continue
        for e in feed.entries[:40]:
            link = e.get("link")
            if not link or link in seen:
                continue
            ts = e.get("published_parsed") or e.get("updated_parsed")
            if ts and time.mktime(ts) < border:
                continue
            summary = re.sub(r"<[^>]+>", " ", e.get("summary", ""))
            summary = re.sub(r"\s+", " ", summary).strip()[:300]
            items.append({"title": e.get("title", "").strip(), "summary": summary, "link": link})
    random.shuffle(items)
    return items[:limit]


def _pixabay(query):
    r = requests.get("https://pixabay.com/api/", params={
        "key": PIXABAY_KEY, "q": query, "image_type": "photo",
        "orientation": "horizontal", "safesearch": "true", "per_page": 30,
    }, timeout=20)
    r.raise_for_status()
    return [h["largeImageURL"] for h in r.json().get("hits", []) if h.get("largeImageURL")]


def _pexels(query):
    r = requests.get("https://api.pexels.com/v1/search",
                     headers={"Authorization": PEXELS_KEY},
                     params={"query": query, "per_page": 20, "orientation": "landscape"},
                     timeout=20)
    r.raise_for_status()
    return [p["src"]["large"] for p in r.json().get("photos", [])]


def find_image(query, exclude=None):
    if not query:
        return None
    sources = [(n, f) for n, f, key in (("Pixabay", _pixabay, PIXABAY_KEY),
                                        ("Pexels", _pexels, PEXELS_KEY)) if key]
    # если по точному запросу ничего нет — пробуем по первым словам
    words = query.split()
    queries = [query] + ([" ".join(words[:2])] if len(words) > 2 else [])
    for name, fn in sources:
        for qq in queries:
            try:
                photos = fn(qq)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: %s", name, e)
                break
            photos = [p for p in photos if p != exclude] or photos
            if photos:
                return random.choice(photos)
    return None

# ─────────────────────────── Генерация поста ───────────────────────────


def parse_answer(raw):
    """Разбирает ответ модели на (номер источника, запрос для фото, текст поста)."""
    src = re.search(r"ИСТОЧНИК:\s*(\d+)", raw)
    photo = re.search(r"ФОТО:\s*(.+)", raw)
    text = raw.split("---", 1)[1] if "---" in raw else re.sub(r"^(ИСТОЧНИК|ФОТО):.*$", "", raw, flags=re.M)
    return (int(src.group(1)) if src else 0,
            photo.group(1).strip().strip("<>\"'") if photo else "small business",
            text.strip())


def generate(rubric_idx):
    name, task = RUBRICS[rubric_idx]
    news = fresh_news()
    recent = [r["text"].split("\n", 1)[0][:100] for r in
              q("SELECT text FROM posts WHERE status!='deleted' ORDER BY id DESC LIMIT 40")]

    parts = [f"Рубрика: {name}. Задача: {task}."]
    if news:
        parts.append("Свежие новости — выбери одну как повод для идеи, если подходит:")
        parts += [f"{i}. {n['title']} — {n['summary']}" for i, n in enumerate(news, 1)]
    if recent:
        parts.append("Эти темы уже были, не повторяйся:\n" + "\n".join(f"- {t}" for t in recent))
    parts.append(POST_FORMAT)

    src, photo_query, text = parse_answer(giga.chat(SYSTEM_PROMPT, "\n\n".join(parts)))
    source = news[src - 1] if 0 < src <= len(news) else None
    if source:
        q("INSERT OR IGNORE INTO seen_news (link, at) VALUES (?,?)", (source["link"], now().isoformat()))
    return {"text": text, "photo_query": photo_query,
            "image_url": find_image(photo_query),
            "source_url": source["link"] if source else None}

# ─────────────────────────── Модерация ───────────────────────────


def card(p, header=None):
    name = RUBRICS[p["rubric"]][0]
    when = f"слот {p['slot_time']}" if p["slot_time"] else "вне расписания"
    lines = [header or f"📝 Черновик №{p['id']} · {name} · {when}"]
    if p["source_url"]:
        lines.append(f"Повод: {p['source_url']}")
    lines += ["━━━━━━━━━━", p["text"]]
    return "\n".join(lines)


def moderation_kb(p):
    pid = p["id"]
    first = [("⚡ Сейчас", f"now:{pid}")]
    if p["slot_time"]:
        first.insert(0, (f"✅ В {p['slot_time']}", f"pub:{pid}"))
    return keyboard([first,
                     [("🔄 Текст", f"regen:{pid}"), ("🖼 Фото", f"photo:{pid}")],
                     [("✏️ Править", f"edit:{pid}"), ("🗑 Удалить", f"del:{pid}")]])


def atts(p, with_kb=True):
    a = [image_att(p["image_url"])] if p["image_url"] else []
    if with_kb:
        a.append(moderation_kb(p))
    return a


def send_card(p, user_ids=None):
    for uid in (user_ids or ADMIN_IDS):
        try:
            mx.send(card(p), user_id=uid, attachments=atts(p))
        except Exception as e:  # noqa: BLE001
            log.error("Не удалось отправить черновик админу %s: %s", uid, e)


def create_draft(slot_key, slot_time, rubric_idx):
    data = generate(rubric_idx)
    pid = insert_post(slot_key=slot_key, slot_time=slot_time, rubric=rubric_idx, **data)
    send_card(get_post(pid))
    log.info("Черновик №%s (%s) отправлен админам", pid, slot_key)
    return pid


def publish(p):
    mx.send(p["text"], chat_id=CHANNEL_ID, attachments=atts(p, with_kb=False) or None)
    update_post(p["id"], status="published")
    log.info("Пост №%s опубликован", p["id"])


def slot_dt(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


edit_wait = {}  # user_id → id поста, для которого ждём новый текст


def in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def on_callback(u):
    cb = u.get("callback") or u
    cid = cb.get("callback_id")
    uid = (cb.get("user") or {}).get("user_id")
    action, _, pid = (cb.get("payload") or "").partition(":")
    if uid not in ADMIN_IDS or not pid.isdigit():
        return
    p = get_post(int(pid))
    if not p:
        return
    if p["status"] in ("published", "deleted"):
        done = "✅ Уже опубликован" if p["status"] == "published" else "🗑 Уже удалён"
        mx.answer(cid, card(p, f"{done} (№{p['id']})"), atts(p, with_kb=False))
        return

    if action == "pub":
        at = slot_dt(now(), p["slot_time"])
        if at < now() - timedelta(minutes=1):
            at = now()
        update_post(p["id"], status="approved", publish_at=at.isoformat())
        mx.answer(cid, card(p, f"✅ Одобрено, выйдет в {at:%H:%M} (№{p['id']})"), atts(p, with_kb=False))

    elif action == "now":
        mx.answer(cid, card(p, f"⏳ Публикую №{p['id']}…"), atts(p, with_kb=False))
        try:
            publish(p)
            mx.send(f"✅ Пост №{p['id']} опубликован в канале", user_id=uid)
        except Exception as e:  # noqa: BLE001
            update_post(p["id"], status="failed")
            mx.send(f"⚠️ Не удалось опубликовать №{p['id']}: {e}", user_id=uid)

    elif action == "regen":
        mx.answer(cid, card(p, f"⏳ Переписываю №{p['id']}…"), atts(p, with_kb=False))

        def job():
            try:
                update_post(p["id"], **generate(p["rubric"]))
                send_card(get_post(p["id"]), [uid])
            except Exception as e:  # noqa: BLE001
                mx.send(f"⚠️ Не получилось переписать: {e}", user_id=uid)
        in_background(job)

    elif action == "photo":
        new = find_image(p["photo_query"], exclude=p["image_url"])
        update_post(p["id"], image_url=new or p["image_url"])
        p = get_post(p["id"])
        mx.answer(cid, card(p), atts(p))

    elif action == "edit":
        edit_wait[uid] = p["id"]
        mx.answer(cid, card(p), atts(p))
        mx.send(f"✏️ Пришлите исправленный текст поста №{p['id']} одним сообщением.\n"
                "Чтобы отменить — отправьте /cancel", user_id=uid)

    elif action == "del":
        update_post(p["id"], status="deleted")
        mx.answer(cid, card(p, f"🗑 Удалено (№{p['id']})"), [])


HELP = ("Команды:\n"
        "/new — сделать черновик прямо сейчас (рубрика по кругу)\n"
        "/new 3 — черновик конкретной рубрики (1–5)\n"
        "/queue — что ждёт модерации и публикации\n"
        "/chats — ID каналов, где состоит бот\n"
        "/cancel — отменить правку текста\n\n"
        "Рубрики:\n" + "\n".join(f"{i}. {r[0]}" for i, r in enumerate(RUBRICS, 1)))


known_chats = {}  # chat_id → описание, чтобы сообщать ID канала один раз


def report_chat(chat_id, kind):
    if not chat_id or chat_id in known_chats or chat_id == CHANNEL_ID:
        return
    title = ""
    try:
        title = mx.call("GET", f"/chats/{chat_id}").get("title") or ""
    except Exception:  # noqa: BLE001
        pass
    known_chats[chat_id] = f"{title or 'без названия'} ({kind or 'чат'})"
    log.info("Вижу канал/чат %s: CHANNEL_ID=%s", known_chats[chat_id], chat_id)
    for a in ADMIN_IDS:
        try:
            mx.send(f"Вижу канал/чат «{title or 'без названия'}». Его ID: `{chat_id}`\n"
                    "Если это «Идея в кассу», впишите число в CHANNEL_ID и перезапустите бота.",
                    user_id=a)
        except Exception:  # noqa: BLE001
            pass


def on_message(u):
    msg = u.get("message") or {}
    rcp = msg.get("recipient") or {}
    if rcp.get("chat_type") not in (None, "dialog"):
        report_chat(rcp.get("chat_id"), rcp.get("chat_type"))
        return  # на сообщения в каналах и группах не отвечаем
    uid = (msg.get("sender") or {}).get("user_id")
    text = ((msg.get("body") or {}).get("text") or "").strip()
    if not uid:
        return
    if uid not in ADMIN_IDS:
        if not ADMIN_IDS:  # режим настройки: админы ещё не заданы — подсказываем ID
            mx.send(f"Ваш ID в MAX: `{uid}`\nВпишите его в ADMIN_IDS и перезапустите бота.", user_id=uid)
        else:
            log.info("Посторонний пользователь %s написал боту — игнорирую", uid)
        return

    if text == "/cancel":
        edit_wait.pop(uid, None)
        mx.send("Правка отменена.", user_id=uid)
    elif uid in edit_wait and not text.startswith("/"):
        pid = edit_wait.pop(uid)
        update_post(pid, text=text)
        send_card(get_post(pid), [uid])
    elif text.startswith("/new"):
        arg = text[4:].strip()
        rubric = int(arg) - 1 if arg.isdigit() and 1 <= int(arg) <= len(RUBRICS) \
            else int(time.time() // 60) % len(RUBRICS)
        mx.send(f"⏳ Готовлю черновик: {RUBRICS[rubric][0]}…", user_id=uid)

        def job():
            try:
                create_draft(f"manual-{time.time_ns()}", None, rubric)
            except Exception as e:  # noqa: BLE001
                mx.send(f"⚠️ Не получилось: {e}", user_id=uid)
        in_background(job)
    elif text == "/chats":
        if known_chats:
            mx.send("Каналы, которые видел бот:\n" + "\n".join(
                f"• {t}: `{c}`" for c, t in known_chats.items()) +
                "\n\nЧисло в кавычках впишите в CHANNEL_ID.", user_id=uid)
        else:
            mx.send("Пока не видел ни одного канала. Добавьте бота администратором канала "
                    "и опубликуйте там любой пост — бот пришлёт ID.", user_id=uid)
    elif text == "/queue":
        rows = q("SELECT * FROM posts WHERE status IN ('pending','approved') ORDER BY id")
        if not rows:
            mx.send("Очередь пуста.", user_id=uid)
        else:
            lines = []
            for r in rows:
                first = r["text"].split("\n", 1)[0].replace("*", "")[:60]
                st = (f"выйдет в {datetime.fromisoformat(r['publish_at']):%H:%M}"
                      if r["status"] == "approved" else "ждёт модерации")
                lines.append(f"№{r['id']} · {st} · {first}")
            mx.send("\n".join(lines), user_id=uid)
    else:
        mx.send(HELP, user_id=uid)

# ─────────────────────────── Планировщик ───────────────────────────


def scheduler_tick():
    t = now()
    # 1) Готовим черновики к слотам
    for i, hhmm in enumerate(SLOTS):
        at = slot_dt(t, hhmm)
        if at - timedelta(minutes=PREP_MINUTES) <= t < at:
            key = f"{t:%Y-%m-%d} {hhmm}"
            if not q("SELECT 1 FROM posts WHERE slot_key=?", (key,), one=True):
                rubric = (i + t.weekday()) % len(RUBRICS)
                try:
                    create_draft(key, hhmm, rubric)
                except Exception as e:  # noqa: BLE001
                    log.error("Не удалось подготовить слот %s: %s", key, e)
                    for uid in ADMIN_IDS:
                        try:
                            mx.send(f"⚠️ Не получилось подготовить пост на {hhmm}: {e}", user_id=uid)
                        except Exception:  # noqa: BLE001
                            pass
    # 2) Публикуем одобренные
    for p in q("SELECT * FROM posts WHERE status='approved'"):
        if datetime.fromisoformat(p["publish_at"]) <= t:
            try:
                publish(p)
            except Exception as e:  # noqa: BLE001
                log.error("Публикация №%s: %s", p["id"], e)
                update_post(p["id"], status="failed")
                for uid in ADMIN_IDS:
                    mx.send(f"⚠️ Не удалось опубликовать №{p['id']}: {e}", user_id=uid)


def scheduler_loop():
    while True:
        try:
            scheduler_tick()
        except Exception as e:  # noqa: BLE001
            log.exception("Планировщик: %s", e)
        time.sleep(30)

# ─────────────────────────── Запуск ───────────────────────────


def main():
    init_db()
    if not CHANNEL_ID:
        log.warning("CHANNEL_ID не задан: черновики будут приходить, но публикация не сработает. "
                    "Опубликуйте любой пост в канале — бот пришлёт админам его ID.")
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: напишите боту в личку — он ответит вашим ID")

    threading.Thread(target=scheduler_loop, daemon=True).start()
    log.info("Бот запущен. Слоты: %s, черновики за %s мин", ", ".join(SLOTS), PREP_MINUTES)

    marker = None
    while True:
        try:
            data = mx.updates(marker)
        except Exception as e:  # noqa: BLE001
            log.error("Получение обновлений: %s", e)
            time.sleep(5)
            continue
        marker = data.get("marker", marker)
        for u in data.get("updates", []):
            try:
                kind = u.get("update_type")
                if kind == "message_callback":
                    on_callback(u)
                elif kind == "message_created":
                    on_message(u)
                elif kind == "bot_added":
                    report_chat(u.get("chat_id"), "канал" if u.get("is_channel") else "чат")
                elif kind in ("message_edited", "message_removed", "user_added", "chat_title_changed"):
                    report_chat(u.get("chat_id") or ((u.get("message") or {}).get("recipient") or {}).get("chat_id"), None)
                elif kind == "bot_started":
                    uid = (u.get("user") or {}).get("user_id")
                    if uid in ADMIN_IDS:
                        mx.send(f"Привет! Бот на связи.\n\n{HELP}", user_id=uid)
                    elif uid and not ADMIN_IDS:
                        mx.send(f"Ваш ID в MAX: `{uid}`\nВпишите его в ADMIN_IDS и перезапустите бота.",
                                user_id=uid)
            except Exception as e:  # noqa: BLE001
                log.exception("Обработка события: %s", e)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "test":
        init_db()
        create_draft(f"manual-{time.time_ns()}", None, 0)
    else:
        main()
