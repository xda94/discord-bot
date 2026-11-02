import asyncio
import sqlite3

import discord

import db


CATALOG = {
    "failed": ("I could not complete this action. Please try again.", "Nu am putut finaliza acțiunea. Încearcă din nou."),
    "wishlist_missing": ("That URL is not in your saved wishlist. Add it with /wishlist-item first.", "Linkul nu există în lista ta salvată. Adaugă-l întâi cu /wishlist-item."),
    "url_invalid": ("Provide a URL of at most 1,500 characters.", "Scrie un link de cel mult 1.500 de caractere."),
    "purchase_advocate": ("Purchase advocate", "Avocatul cumpărăturii"),
    "household_accountant": ("Household accountant", "Contabilul casei"),
    "purchase_current": ("Current saved price: **{price}**", "Preț actual înregistrat: **{price}**"),
    "purchase_stats": ("Lowest: **{minimum}** · Highest: **{maximum}** · Median: **{median}**", "Minim: **{minimum}** · Maxim: **{maximum}** · Mediană: **{median}**"),
    "purchase_comparison": ("{median_delta} vs median · {minimum_delta} vs lowest recorded price", "{median_delta} față de mediană · {minimum_delta} față de minimul înregistrat"),
    "purchase_verdict": ("**Verdict: {verdict}** — {reason}", "**Verdict: {verdict}** — {reason}"),
    "purchase_observations": ("{count} recorded prices · Last check: {checked}", "{count} prețuri înregistrate · Ultima verificare: {checked}"),
    "purchase_buy": ("Worth buying at this price", "Merită la prețul acesta"),
    "purchase_wait": ("Wait", "Așteaptă"),
    "purchase_neutral": ("An ordinary price", "Preț obișnuit"),
    "purchase_refresh": ("Check before buying", "Verifică înainte să cumperi"),
    "purchase_unavailable": ("Wait for restock", "Așteaptă reaprovizionarea"),
    "purchase_insufficient": ("Not enough data", "Date insuficiente"),
    "purchase_reason_buy": ("The price is below the historical median, or at the recorded minimum.", "Prețul este sub mediana istorică sau la minimul înregistrat."),
    "purchase_reason_wait": ("The price is above the historical median. Aim for {median} or less.", "Prețul este peste mediana istorică. Țintește {median} sau mai puțin."),
    "purchase_reason_neutral": ("The price is at the median, or the recorded range varies by less than 1%; there is no clear bargain.", "Prețul este la mediană sau intervalul înregistrat variază cu mai puțin de 1%; nu este o ofertă clară."),
    "purchase_reason_refresh": ("The saved check is missing or over a day old, or stock is unknown. Run /wishlist-refresh first.", "Verificarea salvată lipsește sau e mai veche de o zi, ori stocul este necunoscut. Rulează întâi /wishlist-refresh."),
    "purchase_reason_unavailable": ("The latest saved observation shows it is out of stock.", "Ultima observație salvată arată că produsul nu este în stoc."),
    "purchase_reason_insufficient": ("A valid current price, known currency and at least seven recorded prices are needed for a verdict.", "Pentru verdict sunt necesare un preț actual valid, moneda cunoscută și cel puțin șapte prețuri înregistrate."),
    "purchase_advocate_buy": ("Buy if you already wanted it; this is a favorable price in its recorded history.", "Cumpără dacă îl voiai deja; prețul este favorabil în istoricul înregistrat."),
    "purchase_accountant_buy": ("Buy only if it fits your budget. The history supports the price, not an unplanned expense.", "Cumpără doar dacă se încadrează în buget. Istoricul susține prețul, nu o cheltuială neplanificată."),
    "purchase_advocate_wait": ("Keep it on your wishlist and wait for a price drop; enthusiasm can wait too.", "Păstrează-l în wishlist și așteaptă o scădere; și entuziasmul poate aștepta."),
    "purchase_accountant_wait": ("Do not buy at this price. Aim for {median} or less.", "Nu cumpăra la prețul acesta. Țintește {median} sau mai puțin."),
    "purchase_advocate_neutral": ("Buy only if you need it now; the price is not a special offer.", "Cumpără doar dacă îți trebuie acum; prețul nu este o ofertă specială."),
    "purchase_accountant_neutral": ("Do not rush. Set /wishlist-target-price near the recorded minimum and wait for an alert.", "Nu te grăbi. Setează /wishlist-target-price aproape de minimul înregistrat și așteaptă alerta."),
    "purchase_advocate_refresh": ("Refresh the item first; a stale price cannot tell you whether to order today.", "Actualizează întâi produsul; un preț vechi nu îți spune dacă să comanzi azi."),
    "purchase_accountant_refresh": ("Use /wishlist-refresh, then run /worth-it again before spending.", "Folosește /wishlist-refresh, apoi rulează /worth-it din nou înainte să cheltuiești."),
    "purchase_advocate_unavailable": ("Wait for restock; keep the item tracked.", "Așteaptă reaprovizionarea; păstrează produsul urmărit."),
    "purchase_accountant_unavailable": ("Enable /wishlist-restock-only to receive a restock notification.", "Activează /wishlist-restock-only pentru o notificare la reaprovizionare."),
    "purchase_advocate_insufficient": ("Keep tracking the item before deciding; a convincing sales pitch is not price history.", "Mai urmărește produsul înainte să decizi; un discurs convingător nu ține loc de istoric."),
    "purchase_accountant_insufficient": ("Collect more observations and check the saved price and currency before buying.", "Strânge mai multe observații și verifică prețul și moneda salvate înainte să cumperi."),
    "unknown": ("unknown", "necunoscut"),
}


def text(key, language, **values):
    return CATALOG[key][language == "ro"].format(**values)


def purchase_voices(language, facts, comments=None):
    decision = facts.get("decision", "insufficient")
    median = facts.get("median_display", text("unknown", language))
    lines = []
    for role, label in (("advocate", "purchase_advocate"), ("accountant", "household_accountant")):
        action = text(f"purchase_{role}_{decision}", language, median=median)
        extra = " " + safe_text(comments[role].strip(), 350) if comments else ""
        lines.append(f"**{text(label, language)}:** {action}{extra}")
    return "\n".join(lines)


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

