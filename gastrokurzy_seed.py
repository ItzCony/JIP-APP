"""Seed dat modulu Gastrokurzy – vygenerováno z Excelů zadání.

Zdroje:  KURZY_2026_termíny_a_přehled_kurzů_PODZIM (2).xlsx  (běžné kurzy)
         Tabulka_obsazenosti_kurzu_Praha NESTLE UNILEVER ORKLA.xlsx  (TOP kurzy)

Počty přihlášených z Excelu se zakládají jako anonymní řádky prezenční
listiny (jméno se doplní později) – počet v mřížce = počet aktivních řádků.
Importuje se jen jednou, viz intranet_gastrokurzy.naseeduj().
"""

TERMINY = [
    {'misto': 'Praha', 'datum': '2026-09-09', 'nazev': 'Steaky', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 1), ('praha', 7)]},
    {'misto': 'Praha', 'datum': '2026-09-10', 'nazev': 'Steaky', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('pardubice', 2), ('praha', 2)]},
    {'misto': 'Praha', 'datum': '2026-09-16', 'nazev': 'Balkánská kuchyně', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('praha', 6), ('pardubice', 2), ('ceske_budejovice', 4), ('plzen', 2), ('most', 2)]},
    {'misto': 'Praha', 'datum': None, 'nazev': 'Balkánská kuchyně', 'lektor': 'Coisa', 'stav': 'zruseno', 'prihlasky': []},
    {'misto': 'Praha', 'datum': '2026-10-06', 'nazev': 'Dýně na 100 způsobů', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('pardubice', 2), ('praha', 3), ('ceske_budejovice', 3)]},
    {'misto': 'Praha', 'datum': '2026-10-16', 'nazev': 'Vietnamské závitky', 'lektor': 'Nyung', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 3), ('pardubice', 2), ('ceske_budejovice', 0), ('plzen', 3)]},
    {'misto': 'Praha', 'datum': '2026-10-27', 'nazev': 'Francouzská žážitková kuchyně', 'lektor': 'E. Rivero', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 3), ('praha', 1), ('ceske_budejovice', 3), ('nova_role', 6), ('liberec', 6)]},
    {'misto': 'Praha', 'datum': '2026-10-29', 'nazev': 'Mexico', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 4), ('pardubice', 2), ('praha', 2), ('ceske_budejovice', 4)]},
    {'misto': 'Praha', 'datum': '2026-10-30', 'nazev': 'Cateringové dezerty', 'lektor': 'D.Combi', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 12), ('praha', 1), ('ceske_budejovice', 4), ('plzen', 2), ('nova_role', 4), ('liberec', 3)]},
    {'misto': 'Praha', 'datum': '2026-11-03', 'nazev': 'Španělské 4chodové menu', 'lektor': 'Martin', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 4), ('most', 2), ('liberec', 1)]},
    {'misto': 'Praha', 'datum': '2026-11-09', 'nazev': 'Typická italská hlavní jídla', 'lektor': None, 'stav': 'aktivni', 'prihlasky': [('jilemnice', 4)]},
    {'misto': 'Praha', 'datum': '2026-11-10', 'nazev': None, 'lektor': None, 'stav': 'aktivni', 'prihlasky': []},
    {'misto': 'Praha', 'datum': '2026-11-12', 'nazev': 'Mexico', 'lektor': 'E.Rivero', 'stav': 'aktivni', 'prihlasky': [('pardubice', 1), ('jilemnice', 1), ('most', 2), ('liberec', 1)]},
    {'misto': 'Praha', 'datum': '2026-11-13', 'nazev': 'Zbožíznalství steaky', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('ceske_budejovice', 0)]},
    {'misto': 'Praha', 'datum': '2026-11-18', 'nazev': 'Zvěřina', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 3)]},
    {'misto': 'Praha', 'datum': '2026-11-19', 'nazev': 'Zvěřina', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('pardubice', 2), ('jilemnice', 1)]},
    {'misto': 'Praha', 'datum': '2026-11-24', 'nazev': 'Maso trochu jinak, jehněčí, králik, zajíc, atd.', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('pardubice', 3), ('liberec', 2)]},
    {'misto': 'Praha', 'datum': '2026-11-25', 'nazev': None, 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': []},
    {'misto': 'Praha', 'datum': '2026-11-30', 'nazev': 'Základy české kuchyně-omáčky', 'lektor': 'Kalhous', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 6)]},
    {'misto': 'Praha', 'datum': '2026-12-01', 'nazev': 'Drůbež na talíři, trochu jinak', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 3), ('pardubice', 2)]},
    {'misto': 'Praha', 'datum': '2026-12-08', 'nazev': 'Francouzské vánoční menu', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('jilemnice', 4), ('liberec', 2)]},
    {'misto': 'Praha', 'datum': '2026-12-09', 'nazev': 'Francouzské vánoční menu', 'lektor': 'Dalibor', 'stav': 'aktivni', 'prihlasky': [('pardubice', 3)]},
    {'misto': 'Ostrava', 'datum': '2026-09-15', 'nazev': 'Steaky', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('ostrava', 21)]},
    {'misto': 'Ostrava', 'datum': '2026-09-22', 'nazev': 'Houby kam se podíváš', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('olomouc', 1)]},
    {'misto': 'Ostrava', 'datum': '2026-09-29', 'nazev': 'Dýně na 100 způsobů', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('ostrava', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-10-09', 'nazev': 'Balkánská kuchyně', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('ostrava', 1), ('olomouc', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-10-15', 'nazev': 'Typická italská hlavní jídla', 'lektor': 'Coisa', 'stav': 'aktivni', 'prihlasky': [('ostrava', 5), ('olomouc', 4)]},
    {'misto': 'Ostrava', 'datum': '2026-10-16', 'nazev': 'Typická italská hlavní jídla', 'lektor': 'D.Combi', 'stav': 'aktivni', 'prihlasky': [('olomouc', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-10-26', 'nazev': 'Cateringové dezerty', 'lektor': 'D.Combi', 'stav': 'aktivni', 'prihlasky': [('ostrava', 4), ('olomouc', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-10-27', 'nazev': 'Svatý Martin', 'lektor': 'Martin', 'stav': 'aktivni', 'prihlasky': [('ostrava', 1)]},
    {'misto': 'Ostrava', 'datum': '2026-10-29', 'nazev': 'Francouzská žážitková kuchyně', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('olomouc', 3), ('ostrava', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-11-10', 'nazev': 'Mexico', 'lektor': 'Emmanuel', 'stav': 'aktivni', 'prihlasky': [('ostrava', 5)]},
    {'misto': 'Ostrava', 'datum': '2026-11-11', 'nazev': None, 'lektor': 'Emmanuel', 'stav': 'aktivni', 'prihlasky': []},
    {'misto': 'Ostrava', 'datum': '2026-11-12', 'nazev': 'Zvěřinové hody', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('ostrava', 4)]},
    {'misto': 'Ostrava', 'datum': '2026-11-19', 'nazev': 'Španělský tapas', 'lektor': 'Martin', 'stav': 'aktivni', 'prihlasky': [('ostrava', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-11-20', 'nazev': 'Základy české kuchyně-omáčky', 'lektor': 'Kalhous', 'stav': 'aktivni', 'prihlasky': [('ostrava', 2)]},
    {'misto': 'Ostrava', 'datum': '2026-11-24', 'nazev': 'Španělské 4chodové menu', 'lektor': 'Martin', 'stav': 'aktivni', 'prihlasky': [('ostrava', 5)]},
    {'misto': 'Ostrava', 'datum': '2026-11-26', 'nazev': 'Burger manie', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('ostrava', 3)]},
    {'misto': 'Ostrava', 'datum': '2026-12-01', 'nazev': 'Ryby a plody z moře', 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': [('ostrava', 5)]},
    {'misto': 'Ostrava', 'datum': '2026-12-08', 'nazev': 'Drůbež na talíři, trochu jinak', 'lektor': 'Martin', 'stav': 'aktivni', 'prihlasky': [('ostrava', 5)]},
    {'misto': 'Ostrava', 'datum': '2026-12-10', 'nazev': None, 'lektor': 'Bambůšek', 'stav': 'aktivni', 'prihlasky': []},
]

TOP_TERMINY = [
    {'misto': 'Praha', 'firma': 'ORKLA', 'nazev': 'Značka CHEF CLUB', 'datum': '2026-10-07', 'kapacita': 20, 'prihlasky': [('nova_role', 12), ('jilemnice', 0)]},
    {'misto': 'Praha', 'firma': 'ORKLA', 'nazev': 'Značka CHEF CLUB', 'datum': '2026-10-08', 'kapacita': 20, 'prihlasky': [('jilemnice', 0)]},
    {'misto': 'Praha', 'firma': 'UNILEVER', 'nazev': 'Komerční Horeca + nemocnice a důchoďáky', 'datum': '2026-09-08', 'kapacita': 20, 'prihlasky': [('nova_role', 12), ('jilemnice', 0), ('praha', 7)]},
    {'misto': 'Praha', 'firma': 'NESTLE', 'nazev': 'Nápoje + novinky', 'datum': '2026-09-22', 'kapacita': 20, 'prihlasky': [('pardubice', 1), ('jilemnice', 1), ('praha', 15)]},
    {'misto': 'Praha', 'firma': 'NESTLE', 'nazev': 'Nápoje + novinky', 'datum': '2026-09-23', 'kapacita': 20, 'prihlasky': [('jilemnice', 0)]},
]
