"""Offline airport/city selection from attributed OurAirports data."""
from functools import lru_cache
import json
from pathlib import Path
from i18n import fold

ALIASES={'bucuresti':'bucharest','londra':'london','paris':'paris','roma':'rome','viena':'vienna','varsovia':'warsaw','praga':'prague','bruxelles':'brussels','atena':'athens','copenhaga':'copenhagen','venetia':'venice','florenta':'florence','lisabona':'lisbon','budapesta':'budapest','cluj':'cluj-napoca'}


@lru_cache(maxsize=1)
def catalog():
    return json.loads((Path(__file__).parent/'data'/'airports.json').read_text())


def search_airports(value):
    term=fold(value).strip()
    term=ALIASES.get(term,term)
    results=[r for r in catalog() if term in fold(' '.join(r.values())) or fold(r['name']).startswith(term+' ')]
    results.sort(key=lambda r:(r['iata_code'].casefold()!=term,r['municipality'].casefold()!=term,r['iata_code']))
    return results[:25]


def resolve_airport(value):
    if len(value)==3 and value.isalpha():
        return value.upper()
    term=ALIASES.get(fold(value).strip(),fold(value).strip())
    exact=[r for r in catalog() if fold(r['municipality'])==term or fold(r['name'])==term or fold(r['name']).startswith(term+' ')]
    if len(exact)==1:
        return exact[0]['iata_code']
    raise ValueError('Select a specific airport from autocomplete; this city is ambiguous or unknown.')
