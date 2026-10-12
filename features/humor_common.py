import asyncio
import sqlite3
from datetime import date

import discord

import db


CATALOG = {
    "guild_only": ("Use this command in a server.", "Folosește această comandă pe un server."),
    "dm_sent": ("I sent the result by DM.", "Am trimis rezultatul prin mesaj privat."),
    "dm_failed": ("I could not send a DM. Enable private messages and try again.", "Nu am putut trimite un mesaj privat. Activează mesajele private și încearcă din nou."),
    "failed": ("I could not complete this action. Please try again.", "Nu am putut finaliza acțiunea. Încearcă din nou."),
    "not_owner": ("This control belongs to another member.", "Acest buton aparține altui membru."),
    "expired": ("This action has expired or is already closed.", "Această acțiune a expirat sau este deja închisă."),
    "saved": ("Saved.", "Am salvat."),
    "accept": ("Accept", "Acceptă"),
    "decline": ("Decline", "Refuză"),
    "cancel": ("Cancel", "Anulează"),
    "guilty": ("Guilty", "Vinovat"),
    "innocent": ("Innocent", "Nevinovat"),
    "neutral": ("Neutral result", "Rezultat neutru"),
    "success": ("Success", "Reușit"),
    "not_yet": ("Not yet", "Încă nu"),
    "completed": ("Completed", "Terminat"),
    "abandon": ("Abandon", "Abandonează"),
    "remove": ("Remove", "Șterge"),
    "excuse_invalid": ("Provide a reason of 1–500 characters and a supported style.", "Scrie un motiv de 1–500 de caractere și alege un stil disponibil."),
    "wishlist_missing": ("That URL is not in your saved wishlist. Add it with /wishlist-item first.", "Linkul nu există în lista ta salvată. Adaugă-l întâi cu /wishlist-item."),
    "url_invalid": ("Provide a URL of at most 1,500 characters.", "Scrie un link de cel mult 1.500 de caractere."),
    "purchase_advocate": ("Purchase advocate", "Avocatul cumpărăturii"),
    "household_accountant": ("Household accountant", "Contabilul casei"),
    "purchase_fallback_advocate": ("The wishlist has already reserved a tiny red carpet. Check the saved facts before inviting the parcel.", "Lista de dorințe a rezervat deja un covor roșu minuscul. Verifică datele salvate înainte să inviți coletul."),
    "purchase_fallback_accountant": ("The household ledger requests a pause for budget approval; enthusiasm is not a receipt.", "Registrul casei cere o pauză pentru aprobarea bugetului; entuziasmul nu ține loc de bon."),
    "saved_item": ("Saved item: {title}", "Produs salvat: {title}"),
    "saved_price": ("Saved price: {price} · native currency: {currency}", "Preț salvat: {price} · moneda originală: {currency}"),
    "saved_stock": ("Saved stock: {stock}", "Stoc salvat: {stock}"),
    "last_check": ("Last check: {checked} · {freshness}", "Ultima verificare: {checked} · {freshness}"),
    "history_summary": ("Valid history samples: {count}; recorded range: {start} → {end}", "Observații istorice valide: {count}; interval salvat: {start} → {end}"),
    "history_stats": ("Recorded price range: {minimum} → {maximum}; minimum: {minimum}; median: {median} (recorded units).", "Interval de preț salvat: {minimum} → {maximum}; minim: {minimum}; mediană: {median} (unitățile înregistrate)."),
    "history_currency": ("Historical currency is not stored; no conversion or comparison with the current currency is assumed.", "Moneda observațiilor istorice nu este salvată; nu presupun conversii sau comparații cu moneda curentă."),
    "history_sparse": ("No price movement can be established from fewer than two observations.", "Nu se poate stabili o evoluție a prețului din mai puțin de două observații."),
    "saved_only": ("Saved observations only; this command does not refresh prices or make purchases.", "Doar observații salvate; comanda nu actualizează prețurile și nu cumpără nimic."),
    "unknown": ("unknown", "necunoscut"),
    "in_stock": ("in stock", "disponibil"),
    "out_of_stock": ("out of stock", "indisponibil"),
    "fresh": ("saved observation, not live", "observație salvată, nu în timp real"),
    "stale": ("stale saved observation (over a day old)", "observație salvată veche (peste o zi)"),
    "horoscope": ("🔮 {day} · {theme}\n**Your office zodiac: {sign}**\n{forecast}\nFor amusement; based on the bot's local calendar date.", "🔮 {day} · {theme}\n**Zodia ta de birou: {sign}**\n{forecast}\nPentru amuzament; folosește data locală a botului."),
}

EXCUSE_STYLES = {
    "corporate": ("Corporate", "Corporatist"),
    "grandmother": ("Grandmother", "Bunică"),
    "gamer": ("Gamer", "Gamer"),
    "public-servant": ("Public servant", "Funcționar public"),
}

EXCUSE_FALLBACKS = {
    "corporate": ("Due to {reason}, delivery has been realigned with a strategic coffee break. Thank you for your synergistic understanding.", "Din cauza {reason}, livrarea a fost realiniată cu o pauză strategică de cafea. Mulțumim pentru înțelegerea sinergică."),
    "grandmother": ("My dear, {reason}. I was ready, but even the soup said we should sit down and take our time.", "Dragul bunicii, {reason}. Eu eram gata, dar până și supa a zis să ne așezăm și să nu ne grăbim."),
    "gamer": ("{reason}. Real life queued a surprise boss fight, and my punctuality is waiting at the respawn screen.", "{reason}. Viața a băgat un boss surpriză, iar punctualitatea mea așteaptă la ecranul de respawn."),
    "public-servant": ("Regarding {reason}, the explanation is pending the explanation's approval. Please return with a copy of your patience.", "Referitor la {reason}, explicația așteaptă aprobarea explicației. Vă rugăm să reveniți cu o copie după răbdare."),
}

QUESTS = (
    {"key": "compliment", "text_en": "Give another member a sincere, friendly compliment.", "text_ro": "Oferă altui membru un compliment sincer și prietenos.", "title_key": "kind-word", "title_en": "Ambassador of Kind Words", "title_ro": "Ambasadorul Vorbelor Bune"},
    {"key": "tidy", "text_en": "Tidy one small corner of your desk and tell the group what improved.", "text_ro": "Fă ordine într-un colț mic al biroului și spune grupului ce s-a îmbunătățit.", "title_key": "desk-guardian", "title_en": "Guardian of the Desk", "title_ro": "Paznicul Biroului"},
    {"key": "stretch", "text_en": "Take a comfortable stretch break, if you can, and share a cheerful update.", "text_ro": "Ia o pauză confortabilă de întindere, dacă poți, și împărtășește o veste veselă.", "title_key": "break-champion", "title_en": "Champion of Breaks", "title_ro": "Campionul Pauzelor"},
    {"key": "recommend", "text_en": "Recommend a song, book, or game you enjoy without spoilers.", "text_ro": "Recomandă o melodie, o carte sau un joc care îți place, fără spoilere.", "title_key": "joy-curator", "title_en": "Curator of Joy", "title_ro": "Curatorul Bucuriei"},
)

HOROSCOPE_THEMES = {
    "general": ("General", "General"),
    "work": ("Work", "Muncă"),
    "gaming": ("Gaming", "Jocuri"),
    "household": ("Household", "Casă"),
}

HOROSCOPE_SIGNS = (
    ("The Coffee Mug", "Cana de Cafea"), ("The Loading Bar", "Bara de Încărcare"),
    ("The Missing Sock", "Șoseta Pierdută"), ("The Quiet Keyboard", "Tastatura Tăcută"),
    ("The Snack Drawer", "Sertarul cu Gustări"), ("The Patient Router", "Routerul Răbdător"),
)

HOROSCOPE_FORECASTS = {
    "general": (("The stars suggest a small pause. Your snack cupboard has seconded the motion.", "Astrele propun o mică pauză. Dulapul cu gustări susține moțiunea."), ("A pleasant surprise awaits somewhere between your plans and your next cup of tea.", "O surpriză plăcută te așteaptă între planurile tale și următoarea cană de ceai."), ("Your lucky move is asking a friendly question before opening another tab.", "Mișcarea norocoasă este să pui o întrebare prietenoasă înainte să mai deschizi un tab.")),
    "work": (("An email may find inner peace if you give it a clear subject line.", "Un email poate găsi liniștea interioară dacă îi dai un subiect clar."), ("The spreadsheet spirits favour saving your work before the coffee pilgrimage.", "Spiritele tabelelor preferă să salvezi munca înainte de pelerinajul la cafea."), ("A short meeting has cosmic potential. The mute button believes in you.", "O ședință scurtă are potențial cosmic. Butonul de mute crede în tine.")),
    "gaming": (("The loading screen predicts patience. Your inventory predicts a little tidying.", "Ecranul de încărcare prezice răbdare. Inventarul prezice puțină ordine."), ("A friendly teammate is your lucky charm; remember to thank them.", "Un coechipier prietenos este talismanul tău; nu uita să-i mulțumești."), ("The next boss is hydration. A comfortable break unlocks the bonus level.", "Următorul boss este hidratarea. O pauză confortabilă deblochează nivelul bonus.")),
    "household": (("The missing sock has entered retrograde. Check behind the laundry basket.", "Șoseta pierdută a intrat în retrograd. Verifică în spatele coșului de rufe."), ("Your dishes seek closure. A small clean corner may restore domestic harmony.", "Vasele tale caută încheiere. Un colț mic și curat poate restabili armonia casei."), ("The fridge light illuminates your destiny: make a list before shopping.", "Lumina frigiderului îți luminează destinul: fă o listă înainte de cumpărături.")),
}


def text(key, language, **values):
    return CATALOG[key][language == "ro"].format(**values)


async def interaction_language(interaction):
    try:
        profile = await asyncio.to_thread(db.get_assistant_profile, interaction.user.id)
    except (sqlite3.Error, OSError, ValueError, TypeError):
        profile = None
    preferred = profile.get("language") if isinstance(profile, dict) else getattr(profile, "language", None)
    if preferred in ("en", "ro"):
        return preferred
    locale = str(getattr(interaction, "locale", "en")).split("-")[0]
    return "ro" if locale == "ro" else "en"


def safe_text(value, limit=1800):
    rendered = discord.utils.escape_markdown(discord.utils.escape_mentions(str(value or "")))
    return rendered if len(rendered) <= limit else rendered[:max(0, limit - 1)] + "…"


def local_day():
    return date.today()


def week_key(day=None):
    year, week, _ = (day or local_day()).isocalendar()
    return f"{year}-W{week:02d}"
