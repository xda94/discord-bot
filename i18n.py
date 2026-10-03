"""Small shared English/Romanian catalog; API identifiers stay language neutral."""
from __future__ import annotations
import re
import unicodedata


def fold(value: str) -> str:
    value = unicodedata.normalize('NFKD', value.casefold().replace('ş', 'ș').replace('ţ', 'ț'))
    return ''.join(c for c in value if not unicodedata.combining(c)).translate(str.maketrans({'–':'-', '—':'-', '‑':'-'}))


def language_for(text: str = '', profile=None, fallback: str = 'en') -> str:
    normalized = fold(text)
    if re.search(r'\b(?:in romanian|in romana|raspunde in romana)\b', normalized):
        return 'ro'
    if re.search(r'\b(?:in english|in engleza|raspunde in engleza)\b', normalized):
        return 'en'
    preferred = getattr(profile, 'language', None) if profile is not None else None
    if isinstance(profile, dict):
        preferred = profile.get('language')
    if preferred in ('en', 'ro'):
        return preferred
    if re.match(r'^(?:please[,:]?\s+)?(?:show|list|remind me|track|watch|monitor|set target|clear target|refresh|graph|compare|notify me|stop tracking|cancel|snooze|edit)\b', normalized):
        return 'en'
    if re.search(r'\b(?:salut|buna|multumesc|ajuta|vreau|cum|ce faci|arata|aminte|aminteste|adu|pune|lista|dorinte|urmareste|pret|lei|maine|azi|ore|zile|saptamani|anunta|cand|sterge|zboruri|te rog|seteaza|actualizeaza|grafic|compara|notificari|monitorizeaza|activeaza|dezactiveaza|peste|sa|nu|pentru|meu)\b', normalized) or re.search('[ăâîșțşţ]', text, re.I):
        return 'ro'
    return fallback


CATALOG = {
 'invalid': ('I could not interpret that action. Use /help for examples.', 'Nu am putut interpreta cererea. Folosește /help pentru exemple.'),
 'failed': ('I could not complete that action. Please try again.', 'Nu am putut finaliza acțiunea. Încearcă din nou.'),
 'private_failed': ('I could not send the private result. Enable DMs or use {command}.', 'Nu am putut trimite rezultatul privat. Activează mesajele private sau folosește {command}.'),
 'timezone': ('Which timezone should I use? Mention me with an IANA name, e.g. Europe/Bucharest or UTC.', 'Ce fus orar să folosesc? Menționează-mă cu un nume IANA, de exemplu Europe/Bucharest sau UTC.'),
 'time': ('What exact date and time should I use? Mention me with a date and HH:MM.', 'La ce dată și oră exactă? Menționează-mă cu o dată și ora HH:MM.'),
 'duration': ('Use a complete positive duration, e.g. 1 hour and 30 minutes.', 'Folosește o durată pozitivă completă, de exemplu 1 oră și 30 de minute.'),
 'reminder_text': ('Reply to this message, mention me, and provide the reminder text.', 'Răspunde la acest mesaj, menționează-mă și scrie ce vrei să-ți amintesc.'),
 'product': ('Which product? Mention me with its URL, ID, or title.', 'Pentru ce produs? Menționează-mă cu linkul, ID-ul sau numele lui.'),
 'price': ('What target price and currency should I use?', 'Ce preț țintă și monedă să folosesc?'),
 'currency': ('Use RON/lei, EUR/euro, DKK, USD, or GBP.', 'Folosește RON/lei, EUR/euro, DKK, USD sau GBP.'),
 'number': ('That number is ambiguous or invalid. Use an ungrouped amount, e.g. 1500.50 (English) or 1500,50 (Romanian).', 'Numărul este ambiguu sau invalid. Scrie suma fără separator de mii, de exemplu 1500,50.'),
 'multiple': ('Please request one action at a time.', 'Cere o singură acțiune pe mesaj.'),
 'cancelled': ('The unfinished request was cancelled.', 'Am anulat cererea neterminată.'),
 'expired': ('That request expired. Mention me with the full request again.', 'Cererea a expirat. Menționează-mă din nou cu cererea completă.'),
 'not_found': ('No matching item was found in your list.', 'Nu am găsit un element potrivit în lista ta.'),
 'select': ('Choose the matching product below.', 'Alege produsul potrivit mai jos.'),
 'not_owner': ('This control belongs to another user.', 'Acest buton aparține altui utilizator.'),
 'reminders': ('Your reminders', 'Reminderele tale'),
 'delivery_permission': ('Delivery was blocked by permissions or an inaccessible destination.', 'Trimiterea a fost blocată de permisiuni sau de o destinație inaccesibilă.'),
 'delivery_uncertain': ('Delivery is unconfirmed. Check the channel before retrying.', 'Trimiterea nu este confirmată. Verifică mesajele din canal înainte de a reîncerca.'),
 'delivery_error': ('Delivery failed ({reason}).', 'Trimiterea a eșuat ({reason}).'),
 'empty_reminders': ('You have no saved reminders.', 'Nu ai remindere salvate.'),
 'reminder_delivery': ('🔔 <@{user_id}>, here is your reminder: **{message}**', '🔔 <@{user_id}>, îți amintesc: **{message}**'),
 'saved': ('Saved.', 'Am salvat.'),
 'stop_tracking': ('Stop tracking', 'Oprește urmărirea'),
 'retry': ('Retry', 'Reîncearcă'), 'edit': ('Edit', 'Modifică'), 'cancel': ('Cancel', 'Anulează'),
 'snooze': ('Snooze 10 minutes', 'Amână 10 minute'),
 'busy': ("The model is busy right now, so I didn't queue this request. Please try again shortly.", 'Modelul este ocupat. Încearcă din nou în curând.'),
 'wait': ('Please wait {seconds}s before trying again.', 'Așteaptă {seconds}s înainte de a încerca din nou.'),
 'pending': ('I am already working on your request.', 'Lucrez deja la cererea ta.'),
 'unavailable': ("I couldn't generate a reliable answer. Please try again.", 'Nu am putut genera un răspuns sigur. Încearcă din nou.'),
 'start': ('Choose a language and set your timezone before using calendar times. You can also explore a feature below.', 'Alege limba și setează fusul orar înainte de a folosi date și ore. Poți explora o funcție mai jos.'),
 'help': ('Mention me for each command: track <URL>; show my wishlist; remind me in 1 hour and 30 minutes to call home. Use /reminder-list to manage reminders. Choose a category or the complete reference.', 'Menționează-mă la fiecare comandă: urmărește <URL>; arată-mi lista de dorințe; adu-mi aminte peste o oră și 30 de minute să sun acasă. Folosește /reminder-list pentru gestionare. Alege o categorie sau referința completă.'),
 'profile': ('Your assistant profile', 'Preferințele asistentului'),
 'timezone_required': ('Set an explicit timezone with /assistant-profile-set before using calendar times, recurrence, quiet hours, or digests.', 'Setează explicit fusul orar cu /assistant-profile-set înainte de a folosi date și ore, repetări, ore de liniște sau rezumate.'),
 'airport': ('Select an airport; that city has multiple airports.', 'Alege un aeroport; orașul are mai multe aeroporturi.'),
 'observed': ('Observed at {time}', 'Observat la {time}'),
 'digest': ('Your daily tracking digest', 'Rezumatul zilnic al urmăririlor'),
 'price_label': ('Price', 'Preț'),
 'currency_label': ('Currency', 'Monedă'),
 'message_label': ('Message', 'Mesaj'),
 'restock_control': ('Restock only', 'Doar revenire în stoc'),
 'graph_period': ('Last **{days} days** · {mode} · dates in UTC.', 'Ultimele **{days} zile** · {mode} · date în UTC.'),
 'graph_percentage': ('percentage change', 'variație procentuală'),
 'graph_empty': (' No usable observations in this period. Try a longer range. Currency views need a known currency/rate; percentage views need a positive starting price.', ' Nu există observații utilizabile în această perioadă. Încearcă un interval mai lung. Prețurile necesită o monedă și un curs cunoscut; procentele necesită un preț inițial pozitiv.'),
 'graph_skipped': (' Skipped {count} item(s): no observations, unavailable currency conversion, or nonpositive starting price.', ' {count} produse omise: lipsesc observațiile, conversia valutară sau un preț inițial pozitiv.'),
 'graph_showing': (' Showing {count} product(s).', ' Sunt afișate {count} produse.'),
 'graph_baseline': (' Each starts at 0% at its first observation in this period.', ' Fiecare începe de la 0% la prima observație din această perioadă.'),
 'graph_custom': ('Custom graph period', 'Interval personalizat'),
 'graph_days': ('{days} days', '{days} zile'),
 'graph_custom_button': ('Custom days', 'Alt interval'),
 'graph_days_label': ('Number of days (1–{maximum})', 'Număr de zile (1–{maximum})'),
 'graph_days_error': ('Enter a whole number from 1 to {maximum}.', 'Introdu un număr întreg între 1 și {maximum}.'),
 'graph_prices': ('Show prices', 'Arată prețurile'),
 'graph_compare': ('Compare % change', 'Compară variația %'),
 'graph_owner': ('Run your own wishlist graph command to use these controls.', 'Folosește /wishlist-graph pentru propriul grafic și butoanele lui.'),
 'graph_expired': ('These controls expired. Run the graph command again.', 'Butoanele au expirat. Cere din nou graficul.'),
 'graph_busy': ('Your graph is still rendering. Please wait.', 'Graficul încă se generează. Așteaptă puțin.'),
 'graph_failed': ('Could not render the graph. Please try again.', 'Nu am putut genera graficul. Încearcă din nou.'),
 'graph_not_found': ('That URL is not in your tracking list.', 'Linkul nu este în lista ta de produse.'),
 'empty_wishlist': ('You are not tracking any items.', 'Nu urmărești niciun produs.'),
 'chart_time': ('Date / time (UTC)', 'Dată / oră (UTC)'),
 'chart_price': ('Price ({currency})', 'Preț ({currency})'),
 'chart_observations': ('{count} observations', '{count} observații'),
 'chart_summary': ('Current {current} {currency}  •  Lowest {minimum} {currency}  •  Change {change}%', 'Acum {current} {currency}  •  Minim {minimum} {currency}  •  Variație {change}%'),
 'chart_target': ('Target {price} {currency}', 'Țintă {price} {currency}'),
 'chart_change': ('Change from first observation (%)', 'Variație față de prima observație (%)'),
 'chart_title': ('Price evolution — all tracked items', 'Evoluția prețurilor — toate produsele'),
 'chart_products': ('{count} products', '{count} produse'),
 'chart_start': ('Each product starts at 0% in the selected period', 'Fiecare produs începe de la 0% în perioada aleasă'),
 'chart_currency': ('All prices shown in {currency}', 'Toate prețurile sunt afișate în {currency}'),
}


def t(key: str, language: str = 'en', **params) -> str:
    pair = CATALOG.get(key, CATALOG['invalid'])
    return pair[language == 'ro'].format(**params)


# Legacy feature output is migrated at presentation boundaries. Longest labels
# first prevents overlapping replacements. User-authored reminder text never
# passes through this adapter.
LABELS = {
 'You are not tracking any items.':'Nu urmărești niciun produs.',
 'Your tracked items':'Produsele urmărite', 'shown in each item\'s native currency':'în moneda originală a fiecărui produs',
 'converted to':'convertite în', 'Stock unknown':'Stoc necunoscut', 'Out of stock':'Stoc epuizat', 'In stock':'În stoc',
 'Target: none':'Preț țintă: niciunul', 'Target:':'Preț țintă:', 'Price:':'Preț:', 'Source:':'Sursă:',
 'Restock-only':'Doar revenire în stoc', 'Last checked':'Ultima verificare', 'Check failed':'Verificare eșuată',
 'Never checked':'Neverificat', 'Your flight trackers':'Zborurile urmărite', 'First price found':'Primul preț găsit',
 'Price dropped from':'Prețul a scăzut de la', 'Flight tracker':'Urmărire zbor', 'No flight trackers':'Niciun zbor urmărit',
 'Language:':'Limbă:', 'Tone:':'Ton:', 'Currency:':'Monedă:', 'Timezone:':'Fus orar:',
 'Notifications:':'Notificări:', 'LLM behavior:':'Detaliile răspunsurilor:', 'feature default':'implicit',
 'Your assistant profile':'Preferințele asistentului', 'Saved.':'Am salvat.',
 'That URL is already in your tracking list.':'Linkul este deja în lista ta.',
 'The source blocked or timed out, so the URL was not added.':'Site-ul a blocat cererea sau nu a răspuns. Linkul nu a fost adăugat.',
 'The page had no supported price or stock data, so the URL was not added.':'Nu am găsit un preț sau date despre stoc. Linkul nu a fost adăugat.',
 'I couldn\'t save that reminder. Please try again.':'Nu am putut salva reminderul. Încearcă din nou.',
 'That wishlist change could not be applied.':'Nu am putut aplica modificarea listei.',
 'Use minutes, hours, or days for the reminder duration.':'Folosește minute, ore sau zile pentru durată.',
 'Use one of these currencies: RON, DKK, EUR, USD, or GBP.':'Folosește RON/lei, DKK, EUR/euro, USD sau GBP.',
 'Use one URL and no extra instructions.':'Trimite un singur link, fără alte instrucțiuni.',
 'Please provide exactly one HTTP(S) product URL.':'Trimite exact un link HTTP(S) de produs.',
}


LABELS.update({
    "Refreshed the requested item.": "Am actualizat produsul cerut.",
    "Refreshed:": "Actualizat:", "Refreshed": "Am actualizat",
    "tracked item(s).": "produse urmărite.", "of": "din",
    "Refresh failed unexpectedly:": "Actualizarea a eșuat neașteptat:",
    "Refresh failed:": "Actualizarea a eșuat:",
    "blocked or could not be reached.": "a blocat cererea sau nu este accesibil.",
    "returned no supported price/stock data.": "nu a oferit date utilizabile despre preț sau stoc.",
    "source blocked/unreachable": "sursă blocată sau inaccesibilă", "source unsupported": "sursă nesuportată",
    "not yet": "încă neverificat", "not reached": "neatins", "conversion unavailable": "conversie indisponibilă",
    "That item is still on cooldown.": "Produsul este încă în perioada de așteptare.",
    "All your tracked items are still on cooldown.": "Toate produsele sunt încă în perioada de așteptare.",
    "Try again in up to": "Încearcă din nou în cel mult",
    "Skipped": "Omise", "item(s) still on cooldown": "produse încă în așteptare", "remaining": "rămase",
    "That is not a valid HTTP(S) URL.": "Linkul HTTP(S) nu este valid.",
    "The URL could not be added.": "Nu am putut adăuga linkul.",
    "The page was read, but the database could not save it. Please try again.": "Am citit pagina, dar nu am putut salva produsul. Încearcă din nou.",
    "Update:": "Actualizare:", "Price changed:": "Preț modificat:",
    "Item is now **BACK IN STOCK**!": "Produsul a **REVENIT ÎN STOC**!",
    "Target reached. Now": "Prețul țintă a fost atins. Acum",
    "target:": "țintă:", "back in stock": "revenit în stoc", "target reached": "țintă atinsă",
    "buy window": "moment bun pentru cumpărare", "maybe wait": "poți aștepta",
    "Historical low": "Minim istoric", "Historical high": "Maxim istoric",
    "You are not tracking any flights.": "Nu urmărești niciun zbor.",
    "No price yet": "Preț indisponibil încă", "Last error:": "Ultima eroare:",
    "Budget reached:": "Buget atins:", "Active": "Activ", "Budget:": "Buget:", "Adults:": "Adulți:",
    "exact": "date fixe", "Dates:": "Date:", "Airline(s):": "Companie aeriană:",
    "Outbound stops:": "Escale la plecare:", "not provided": "nespecificat",
    "direct": "direct", "unknown": "necunoscut", "Yes": "Da", "No": "Nu",
    "Unknown": "Necunoscut", "Quiet hours:": "Ore de liniște:",
    "Delivery:": "Trimitere:", "Digest:": "Rezumat:", "Current price:": "Preț actual:",
    "Added:": "Adăugat:", "In stock:": "În stoc:",
    "This link is already in your tracking list.": "Linkul este deja în lista ta.",
    "Link removed and data cleared.": "Am eliminat linkul și datele sale.",
    "Link not found in your list.": "Linkul nu apare în lista ta.",
    "That URL is not in your tracking list.": "Linkul nu apare în lista ta de urmărire.",
    "Target price must be greater than zero.": "Prețul țintă trebuie să fie pozitiv.",
    "Target-price alert removed.": "Am eliminat alerta de preț țintă.",
    "I will notify you once when this item reaches": "Te voi anunța o dată când produsul ajunge la",
    "or less.": "sau mai puțin.",
    "Restock-only mode enabled. Price history still updates, but only a back-in-stock notification will be sent.": "Am activat alertele doar la revenirea în stoc. Istoricul prețurilor se actualizează în continuare.",
    "Restock-only mode disabled. Price and target alerts are enabled again.": "Am reactivat alertele de preț și de preț țintă.",
    "Choose at least one preference to update.": "Alege cel puțin o preferință de modificat.",
    "I couldn't save your assistant profile. Please try again.": "Nu am putut salva preferințele. Verifică fusul orar și intervalul orelor de liniște.",
    "I couldn't reset your assistant profile. Please try again.": "Nu am putut reseta preferințele. Încearcă din nou.",
    "Your assistant profile was reset.": "Am resetat preferințele asistentului.",
    "Timezone must be a valid IANA name such as `Europe/Bucharest` or `UTC`.": "Folosește un fus orar IANA valid, de exemplu `Europe/Bucharest` sau `UTC`.",
    "Times must use HH:MM": "Scrie ora în formatul HH:MM",
    "Invalid tracker:": "Date de urmărire invalide:",
    "Tracker not found in your list.": "Nu am găsit urmărirea în lista ta.",
    "Invalid budget or tracker.": "Buget sau urmărire invalidă.",
    "Budget must be positive and finite.": "Bugetul trebuie să fie un număr pozitiv și finit.",
    "SerpApi login saved. Future flight searches will use your own account.": "Am salvat accesul SerpApi. Căutările viitoare folosesc contul tău.",
    "Add a link to track price and stock": "Adaugă un link pentru urmărirea prețului și stocului",
    "Remove a link from tracking": "Elimină un produs din urmărire",
    "Alert once when an item's price reaches your target": "Primește o alertă când prețul ajunge la ținta ta",
    "Remove a target-price alert from a tracked item": "Elimină alerta de preț țintă",
    "Choose whether an item only notifies when restocked": "Alege alertele doar la revenirea în stoc",
    "Refresh one item by URL, or all items when omitted": "Actualizează un produs sau întreaga listă",
    "Show your tracked items and their current prices": "Arată produsele urmărite și prețurile actuale",
    "Generate a price history graph for a tracked item": "Generează graficul prețurilor unui produs",
    "Combined price history graph for ALL your tracked items": "Compară istoricul prețurilor produselor urmărite",
    "Set a reminder": "Programează un reminder",
    "Show and manage your reminders privately": "Vezi și gestionează reminderele în privat",
    "Edit your reminder": "Modifică un reminder", "Cancel your reminder": "Anulează un reminder",
    "Snooze a reminder for ten minutes": "Amână un reminder cu zece minute",
    "Show your private assistant preferences": "Vezi preferințele asistentului în privat",
    "Update your private assistant preferences": "Modifică preferințele asistentului",
    "Reset your assistant preferences to defaults": "Resetează preferințele asistentului",
    "Add a fixed-date round-trip flight price tracker": "Urmărește prețul unui zbor dus-întors cu date fixe",
    "Show your saved flight trackers": "Arată zborurile urmărite",
    "Delete one of your flight trackers": "Șterge o urmărire de zbor",
    "Set or clear your flight budget threshold": "Setează sau elimină bugetul unui zbor",
    "Set or replace your private SerpApi API key": "Configurează cheia ta privată SerpApi",
    "Remove your saved SerpApi API key": "Elimină cheia SerpApi salvată",
    "Privately set up language, timezone, and explore features": "Configurează limba și fusul orar în privat",
})


class _LocalizedSender:
    """Present legacy feature responses without translating stored content."""
    def __init__(self, sender, user_id, language):
        self._sender = sender
        self._user_id = user_id
        self._language = language

    def __getattr__(self, name):
        target = getattr(self._sender, name)
        if name not in ('send', 'send_message'):
            return target
        async def send(content=None, *args, **kwargs):
            if isinstance(content, str):
                import db
                protected = []
                for row in db.get_user_scraped_items(self._user_id):
                    protected.extend((row[0], f"**{row[3]}**" if row[3] else None))
                protected.extend(re.findall(r'https?://[^\s`]+', content))
                content = localize(content, self._language, protected=protected)
            return await target(content, *args, **kwargs)
        return send


class _LocalizedInteraction:
    def __init__(self, interaction):
        import db
        self._interaction = interaction
        locale = str(getattr(interaction,'locale','en')).split('-')[0]
        self._language = language_for(profile=db.get_assistant_profile(interaction.user.id),fallback='ro' if locale=='ro' else 'en')
        self.response = _LocalizedSender(interaction.response, interaction.user.id, self._language)
        self.followup = _LocalizedSender(interaction.followup, interaction.user.id, self._language)

    def __getattr__(self, name):
        return getattr(self._interaction, name)


def localized_interaction(interaction):
    return interaction if isinstance(interaction, _LocalizedInteraction) else _LocalizedInteraction(interaction)


def localize(text: str, language: str, *, protected=()) -> str:
    if language != 'ro':
        return text
    originals = {}
    for index, value in enumerate(sorted(set(v for v in protected if v), key=len, reverse=True)):
        token = f"\x00{index}\x00"
        if value in text:
            text = text.replace(value, token)
            originals[token] = value
    for source in sorted(LABELS, key=len, reverse=True):
        if source.isalpha():
            text = re.sub(r'(?<![\w/-])'+re.escape(source)+r'(?![\w/-])',lambda match:LABELS[source],text)
        else:
            text = text.replace(source, LABELS[source])
    # Unmigrated parser usage hints are replaced with Romanian guidance.
    if text.startswith(('Use `', 'Include one HTTP', 'Use only one URL', 'Please provide exactly one', 'The reminder duration')):
        return t('invalid', language)
    for token, original in originals.items():
        text = text.replace(token, original)
    return text
