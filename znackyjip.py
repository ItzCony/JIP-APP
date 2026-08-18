"""
Zpětně kompatibilní wrapper pro modul Privátní značky JIP.
Skutečná logika je v znacky_engine.py.

intranet.py používá:
    znackyjip.vykresli_znacky(user_id, user_name, vsechna_prava)

web_main.py používá:
    znackyjip.bg_uzavreni_pripadu()
    os.makedirs('znacky_foto', exist_ok=True)
    app.add_static_files('/znacky_foto', 'znacky_foto')
"""
from znacky_engine import ZnackyEngine

_engine = ZnackyEngine(
    prefix='znacky',                    # → tabulky: znacky_pripad, znacky_varianta … (stejné jako dříve)
    pravo_uzivatel='znacky_uzivatel',   # stávající práva zachována
    pravo_spravce='znacky_spravce',
    nazev='Privátní značky JIP',
    popis='Hlasování o privátních výrobcích',
    log_kategorie='Znacky JIP',
    barva='purple',
    page_path='/znacky',
    pozvani_rezim=True,     # správce vybírá konkrétní hlasující; vidí jen své případy
)

# Zachování veřejného API, na které odkazuje intranet.py / web_main.py
vykresli_znacky       = _engine.vykresli
bg_uzavreni_pripadu   = _engine.bg_uzavreni_pripadu
inicializace_db       = _engine._inicializace_db
