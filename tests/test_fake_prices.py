"""Ads whose price isn't the price, and cars that are cheap for a reason.

The texts are from real ads the watcher had rated "deal"."""

import pytest

from collectors import watch as w
from collectors.store import Store
from collectors.verdicts import (TOO_CHEAP_FLAG, classify, country_flags, text_flags,
                                 too_cheap)

COPART = ("Automobilis bus parduodamas Vokietijos aukcione Lapkričio 11 d. NEIŠSKIRIAMAS PVM "
          "Dabartinis statymas - 8.900€ Aukciono nuoroda - https://www.copart.de/lot/50737024")


@pytest.mark.parametrize("text, expected", [
    (COPART, ["auction bid"]),
    ("AB “LTG Infra” Aukciono būdu parduoda Transporto priemones.", ["auction bid"]),
    ("Dabartinis statymas - 12.100€+PVM", ["auction bid", "price without VAT"]),
    ("Pilnai važiuoja. Kaina +pvm", ["price without VAT"]),
    ("Mašina Klaipėdoje, išmuitinta Kaina plius PVM", ["price without VAT"]),
    ("Tinka ilgesnėms kelionėms. Lietuva+EU+PVM", ["price without VAT"]),
    ("Nurodyta kaina yra pradinis įnašas, likusi suma lizingu", ["down payment"]),
    ("Po autoivykio", ["damaged"]),
    ("Automobilis parvežtas iš Vokietijos Po nedidelio eismo įvykio", ["damaged"]),
    ("Brauktas sonas Visos dalys supirktos", ["damaged"]),
    ("Nestiprus daužimas", ["damaged"]),
    ("Tepalas aušinimo sistemoje Info telefonu", ["engine trouble"]),
    ("PARDUODAMA DALIM ARBA PASIVAZINEJIMUI dokumentu nebus", ["parts", "no documents"]),
    ("Automobilis be perrašymo.", ["no documents"]),
    ("Parduodamas Be numerių. Kaina galutinė", ["no documents"]),
    ("V5 dokumentai, TITANIUM komplektacija", ["UK registration"]),
    ("MOT yra , pilnas v5 4000€ derinama", ["UK registration"]),
])
def test_wording_from_real_ads_is_flagged(text, expected):
    assert text_flags(text) == expected


@pytest.mark.parametrize("text", [
    # the full price is the one shown; the VAT-free one is extra information
    "Kaina su PVM: 11 100 €, be PVM: 9 174 €.",
    "Kaina eksportui / be PVM 9 300 Eur. Kaina registruojant Lietuvoje 11 253 eur su PVM.",
    "Parduoda patikima įmonė! Išskiriamas pvm! Rodoma pilna suma!",
    "kaina 2400 parduoda įmonė, PVM netaikomas.",
    # leasing offered next to a real price: every tenth ad
    "Galima įsigyti išsimokėtinai, be pradinio įnašo! Lizingas/Paskola",
    "Lizingas - pradinis įnašas nuo 10%, metinės palūkanos nuo 1,49%.",
    "galime nupirkti jusu automobili ir uzskaityti kaip pradine imoka.",
    "Perkant IŠSIMOKĖTINAI šio automobilio mėnesinė įmoka tik 114.75 eur/mėn",
    # bought at an auction, or a dealer that also buys at auctions: a normal sale
    "Automobilis pirktas iš nedaužtų automobilių aukciono.",
    "Galime pasirūpinti automobilio pirkimu iš JAV aukcionų Copart, IAAI , Manheim",
    # denials, and words that only look alike
    "Polizinginis automobilis Pilna serviso istorija Be autoįvykių.",
    "apsaugotas nuo nepageidaujamų įvykių",
    "Reikia keisti variklio alyvą ir pagrindinio diržo komplektą.",
    "Volkswagen Golf 2.3 V5 110kW",                # the engine, not the UK document
])
def test_ordinary_wording_is_left_alone(text):
    assert text_flags(text) == []


def test_first_registration_country():
    assert country_flags("JAV") == ["US import"]
    assert country_flags("Anglija") == ["UK registration"]
    assert country_flags("Vokietija") == [] and country_flags(None) == []


def test_an_ad_that_says_its_price_is_not_the_price():
    # 8,000 expected, low end 6,500, asked 5,000: a deal by the numbers alone
    assert classify(5_000, 6_500, 8_000, read=True) == "deal"
    assert classify(5_000, 6_500, 8_000, flags=["auction bid"], read=True) == "not a real price"
    assert classify(5_000, 6_500, 8_000, flags=["price without VAT"]) == "not a real price"
    # a US import is a real price for a car worth less
    assert classify(5_000, 6_500, 8_000, flags=["US import"], read=True) == "problem"


def test_too_cheap_needs_enough_similar_cars_worth_real_money():
    assert too_cheap(7_500, 83, 15_900)                 # the 2018 BMW 520 at 47%
    assert not too_cheap(8_500, 83, 15_900)             # 53%: steep, but possible
    assert not too_cheap(7_500, 3, 15_900)              # three cars prove nothing
    assert not too_cheap(500, 21, 1_200)                # an old car at half of little
    assert not too_cheap(7_500, None, None)


def test_watcher_verdicts_with_similar_cars():
    cheap = dict(details={}, title="BMW 520", description="Tvarkingas", read=True)
    assert w.verdict(9_000, 11_000, 14_000, similar=(40, 2, 13_000), **cheap) == ("deal", [])
    # too few similar cars to confirm
    assert w.verdict(9_000, 11_000, 14_000, similar=(2, 0, 13_000), **cheap)[0] == "maybe"
    # nothing in the ad explains it, and it costs under half of 83 similar cars
    assert w.verdict(7_500, 11_000, 13_738, similar=(83, 0, 15_900), **cheap) == \
        ("not a real price", [TOO_CHEAP_FLAG])
    # a declared defect explains it: damaged, not a fake price
    assert w.verdict(7_500, 11_000, 13_738, {"damage_type": "Daužtas"}, "BMW 520", None, True,
                     (83, 0, 15_900)) == ("damaged", ["Daužtas"])
    # the ad page's first-registration country
    assert w.verdict(9_000, 11_000, 14_000, {"reg_country": "JAV"}, "Ford Fusion", "Tvarkingas",
                     True, (40, 2, 13_000)) == ("problem", ["US import"])
    assert w.verdict(8_900, 21_000, 25_479, {}, "BMW 520", COPART, True, (3, 0, 28_340)) == \
        ("not a real price", ["auction bid"])


def test_recheck_judges_stored_deals_again(tmp_path):
    store = Store(tmp_path / "t.db")
    site = store.site("autoplius")
    w.ensure_tables(store.conn)

    def car(ad_id, price, km=150_000):
        return {"id": ad_id, "title": "BMW 520", "year": 2018, "price_eur": price,
                "mileage_km": km, "fuel": "Dyzelinas", "engine_l": "2.0",
                "link": f"https://x/{ad_id}"}
    # ten similar cars around 15,000-16,800, and three stored "deals"
    site.upsert_many([car(str(100 + i), 15_000 + 200 * i) for i in range(10)], "bmw", "520")
    site.upsert_many([car("1", 12_000), car("2", 15_500), car("3", 8_900)], "bmw", "520")
    site.save_description("1", "Tvarkingas, be defektų", {})
    site.save_description("2", "Tvarkingas", {})
    site.save_description("3", COPART, {})
    for ad_id, price in (("1", 12_000), ("2", 15_500), ("3", 8_900)):
        store.conn.execute(
            """INSERT INTO new_deals (source, id, found_at, make, model, title, year,
                   mileage_km, price, lo, mid, hi, verdict, flags)
               VALUES ('autoplius', ?, '2026-11-09T10:00:00+00:00', 'bmw', '520', 'BMW 520',
                       2018, 150000, ?, 16000, 19000, 23000, 'deal', '')""", (ad_id, price))
    store.conn.commit()

    assert w.recheck_deals(store.conn) == 2
    rows = dict((r[0], r[1:]) for r in store.conn.execute(
        "SELECT id, verdict, flags, similar_n FROM new_deals"))
    assert rows["1"][0] == "deal" and rows["1"][2] == 12        # still cheapest of its 12
    assert rows["2"][0] == "not cheap"                          # half of them cost less
    assert rows["3"][:2] == ("not a real price", "auction bid")
    store.close()
