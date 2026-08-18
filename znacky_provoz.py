"""
Modul Hlasování Provoz — instace generického engine Značky.
Samostatné DB tabulky, práva a úložiště fotografií od modulu Značky JIP.
"""
from znacky_engine import ZnackyEngine

_engine = ZnackyEngine(
    prefix='znacky_provoz',                     # → tabulky: znacky_provoz_pripad, znacky_provoz_varianta …
    pravo_uzivatel='znacky_provoz_uzivatel',
    pravo_spravce='znacky_provoz_spravce',
    nazev='Hlas Provozu',
    popis='Hlasování o provozních případech',
    log_kategorie='Znacky Provoz',
    barva='blue',
    page_path='/provoz',
)

# Veřejné API (používáno z web_main.py a intranet.py)
vykresli            = _engine.vykresli
bg_uzavreni_pripadu = _engine.bg_uzavreni_pripadu
inicializace_db     = _engine._inicializace_db
foto_dir            = _engine.foto_dir
foto_route          = _engine.foto_route
