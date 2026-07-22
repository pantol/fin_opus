# Odpowiedzi na Twoje pytania — 2026-07-22

> Wszystko poniżej jest zweryfikowane w kodzie (podaję pliki i linie). Kolejność = kolejność Twoich pytań.

## TL;DR

1. **Masz rację co do strategii** — jest celowo "mega safe" i w 100% patrzy w przeszłość (trend + momentum, zero prognozowania). Banki dominują strukturalnie: uniwersum to zaledwie 14 aktywnych spółek, z czego 4 banki, a kapitał jest przydzielany... w kolejności listy w `universe.yaml` (a listę otwierają PKO i Pekao).
2. **`rss_feeds_comprehensive.yaml` to plik z INNEGO projektu** (`~/funn/mythos_finance/examples/`). `fin_opus` w ogóle go nie zna. Co więcej: w domyślnym dziennym uruchomieniu newsy i LLM **nie wpływają na żaden sygnał** — sygnały są w 100% techniczne (cena). Dlatego nie widzisz żadnych zagranicznych źródeł w wynikach.
3. **Nowa strategia (np. łapanie noży) = nowy plik YAML** w `config/strategies/`, nie nowy projekt i nie modyfikacja obecnej. Silnik jest generyczny.
4. **Core, którego nie ruszasz**: `app/risk/`, `app/backtest/`, `app/paper/`, `app/features/`, `app/db.py`. **Twoje pole zabaw**: cały katalog `config/` (YAML-e).
5. **Wspólna praca** = GitHub (repo już ma remote i historię PR-ów), nie "wspólny excel". Docelowo: jeden serwer odpala system, każdy użytkownik ma swój profil YAML i swój czat Telegram.
6. **Personalizacja (Twoje kakao / profil Drona)**: dziś nie istnieje (jest jeden zahardkodowany `user_id: default`), ale schemat bazy jest na to gotowy. Rozpisuję niżej minimalny projekt: `config/users/<imię>.yaml` z portfelem zewnętrznym, tematami (El Niño) i własnym chat_id.

---

## 1. Dlaczego strategia kupuje same banki — i czy słusznie czujesz, że "wycenia przeszłość"

Strategia produkcyjna to `config/strategies/trend_momentum.yaml` — **34 linie**. Całość reguł:

- **Wejście** (oba warunki naraz): cena > SMA200 **i** momentum 6M > 0.
- **Wyjście** (którykolwiek): kroczący stop ATR (2.5×ATR14, podnosi się tylko w górę) **lub** cena < SMA200.
- **Ryzyko**: 1% kapitału na trade, max 8 pozycji, max 20% na spółkę, max 40% na sektor, bezpiecznik przy 25% obsunięcia.

Twoja intuicja jest w punkt, z trzech powodów widocznych w kodzie:

1. **Obie reguły wejścia patrzą wyłącznie wstecz** (średnia z 200 sesji + zwrot za 6 miesięcy). Nie ma żadnej wyceny, jakości, przewidywania. W 5-letniej hossie przechodzi przez to sito praktycznie każdy duży walor, który już urósł.
2. **Uniwersum to tylko 14 aktywnych spółek** ([config/universe.yaml](../config/universe.yaml)): 12 z okolic WIG20 + GTC i Boryszew, do tego 3 spółki zdjęte z giełdy (celowo, przeciw survivorship bias). Wśród aktywnych są 4 banki (PKO, Pekao, Alior, Santander). Cokolwiek jest w trendzie na tej liście, *z definicji* będzie bankiem, KGHM-em albo Orange.
3. **Ukryty bias, o którym warto wiedzieć**: nie ma żadnego scoringu/rankingu. Silnik daje tylko binarne ENTER/EXIT/HOLD ([app/strategy/engine.py:34](../app/strategy/engine.py)), a gdy kilka spółek kwalifikuje się naraz, kapitał dostają **w kolejności listy w `universe.yaml`**. Listę otwierają PKO (#1) i Pekao (#2), które niemal wyczerpują 40-procentowy limit sektora bankowego, zanim Alior (#9) i Santander (#10) w ogóle dojdą do głosu — dlatego każdy z nich dostał po 1 akcji.

Drobna korekta do Twojego obrazu portfela: oprócz banków (~40%, przybite do limitu sektora), KGHM (~8.6%) i Orange (~12%), książka ma też **PZU ~19.3%** i **PKN Orlen ~14.3%** — każda z nich większa niż KGHM czy Orange. Finansówka (banki+PZU) to łącznie ~59% książki.

**Czy "wycenianie przeszłości" to wada?** To jest *definicja* trend followingu — on nie prognozuje, tylko zakłada, że trend częściej trwa, niż się odwraca, i broni się zarządzaniem ryzykiem (ciasne stopy, 1% na trade — dzień 5 pokazał to w praktyce: pierwsze zrealizowane straty −985 PLN ≈ dokładnie 1% książki). To jest **baseline**, punkt odniesienia — a nie docelowa "mądrość" systemu. Warstwa patrząca w przód jest zaplanowana (LLM jako features, radar reżimów w Fazie 3 — patrz sekcja 8), ale dziś jest wyłączona i empirycznie niepotwierdzona.

## 2. Czym jest `jak-dziala-aplikacja.md`, a czym jest strategia

[docs/jak-dziala-aplikacja.md](jak-dziala-aplikacja.md) to **dokumentacja całej maszyny** (dane → features → sygnały → ryzyko → papierowe zlecenia → Telegram), a nie strategia. Sama strategia to wspomniany plik `trend_momentum.yaml`. Rozdzielenie jest celowe (reguła nr 7 projektu): **jeden silnik wykonuje dowolny config**. Maszyna się nie zmienia; wymieniasz tylko YAML z regułami.

## 3. `rss_feeds_comprehensive.yaml` — rozwiązanie zagadki braku zagranicznych źródeł

Tu jest sedno nieporozumienia, w trzech warstwach:

1. **Ten plik nie należy do tego projektu.** Leży w `~/funn/mythos_finance/examples/rss_feeds_comprehensive.yaml` — to Twój osobny, starszy projekt (multi-agentowy system LLM na LangGraph, nieruszany od ~połowy czerwca). Grep po całym `fin_opus` nie znajduje ani tej nazwy, ani słowa "mythos" — zero powiązań między projektami.
2. **Nawet w `mythos_finance` ten plik nie jest podpięty** — to czysty przykład/ściąga (35 feedów: SEC, Reuters, Bloomberg, FT, Reddit...). Tamtejszy runtime czyta inną sekcję (`news_sources` w `config.yaml`), też głównie polską.
3. **W `fin_opus` newsy płyną z** [config/news_sources.yaml](../config/news_sources.yaml): 3 aktywne feedy, wszystkie polskie (ESPI/EBI z Bankiera, StockWatch, PAP Biznes). Zero źródeł zagranicznych. **I najważniejsze:** domyślna dzienna pętla (`make signals`) używa strategii `trend_momentum` **bez** warunku LLM — wariant `trend_momentum_llm` (z bramką `llm_score >= 0`) trzeba włączyć jawnie flagą `--strategy`. Czyli dziś newsy i LLM nie mają wpływu na ani jeden sygnał, który widziałeś. Wyniki to czysta technika na 12 polskich spółkach.

Wniosek: to nie jest tak, że "AI nie skorzystało z zagranicznych źródeł" — ono w ogóle nie siedzi przy stole, przy którym zapadają decyzje. Na razie zgodnie z planem (Faza 0+1 = bez LLM; Faza 2 = podłączona hydraulika LLM, czeka na realne dane do testu A/B).

## 4. Własna strategia ("łapanie spadających noży"): nowy YAML, nie nowy projekt

**Nie piszesz osobnego projektu i nie modyfikujesz `trend_momentum.yaml`.** Tworzysz nowy plik `config/strategies/falling_knife.yaml` i silnik go po prostu wykona:

```bash
python -m app.cli backtest --strategy falling_knife   # walk-forward test vs WIG20TR
python -m app.cli signals  --strategy falling_knife   # sygnały paper (uwaga niżej!)
```

Dlaczego nie modyfikować istniejącej? Bo pętla papierowa liczy hash configu ([app/paper/loop.py:89](../app/paper/loop.py)) i **odmówi działania po zmianie reguł** bez jawnego `--accept-config-change` — to celowy bezpiecznik ciągłości track recordu. Zmiana reguł w locie = zerwanie historii.

Prymitywny łapacz noży jest wyrażalny **już dziś** w istniejącej gramatyce:

```yaml
name: falling_knife
version: 1
entry:
  all:
    - {feature: ret_1m, op: lt, value: -0.20}            # spadek >20% w miesiąc
    - {feature: close_vs_sma200, op: lt, value: -0.15}   # >15% pod SMA200
exit:
  any:
    - {type: atr_stop, atr_mult: 3.5}
    - {feature: close_vs_sma50, op: gt, value: 0.0}      # odbicie nad SMA50 = wyjście
risk:
  risk_per_trade: 0.005          # noże = połowa normalnego ryzyka
  atr_mult_stop: 3.5
  max_open_positions: 4
  max_exposure_per_name: 0.10
  max_exposure_per_sector: 0.20
  max_total_exposure: 0.50
  drawdown_circuit_breaker: 0.20
```

Dostępny słownik features ([app/features/compute.py](../app/features/compute.py)): `close`, `ret_1m/3m/6m/12m`, `momentum_6m`, `sma50`, `sma200`, `close_vs_sma50`, `close_vs_sma200`, `atr`, `realized_vol`, `rel_strength_6m` (+ `llm_score`, `llm_relevance` gdy zmaterializowane).

**Czego brakuje na porządny łapacz noży** (to już drobna praca w kodzie, w `app/features/compute.py`, z testami point-in-time):
- obsunięcie od szczytu (np. `drawdown_52w` — naturalny trigger "duży zjazd"),
- oscylator wyprzedania (RSI — brak w ogóle),
- features wolumenowe (kapitulacja) — wolumen jest w danych, ale nie w panelu features,
- wyjście czasowe ("sprzedaj po N sesjach") i take-profit — silnik zna tylko warunki na features + stop ATR,
- ranking przekrojowy ("kup 3 najbardziej wyprzedane") — dziś tylko binarne bramki per spółka.

**Jedno realne ograniczenie:** pętla papierowa prowadzi **jedną książkę na jednego `user_id`** — dwie strategie nie mogą dziś jechać równolegle na tym samym koncie. Backtestować możesz dowolną od ręki; drugi *portfel papierowy* wymaga drugiego `user_id` (schemat bazy to wspiera, brakuje tylko drobnej hydrauliki w CLI). To zresztą dokładnie ten sam mechanizm, który da Dronowi jego własny portfel — patrz sekcja 7.

## 5. Backend: co jest silnikiem (nie ruszać), a co jest Twoje

### Nie ruszać (deterministyczny core; każdy z tych plików strzeże innej gwarancji)

| Plik | Czego strzeże | Co się psuje po edycji |
|---|---|---|
| [app/risk/manager.py](../app/risk/manager.py) | sizing, stopy, limity ekspozycji | złe wielkości pozycji w backteście I paperze naraz |
| [app/backtest/fills.py](../app/backtest/fills.py) | prowizja, spread, poślizg, limit wolumenu | backtest z krainy fantazji |
| [app/backtest/engine.py](../app/backtest/engine.py) | decyzja na zamknięciu T, fill na otwarciu T+1, brak look-ahead | najgroźniejszy błąd w quant tradingu (zaglądanie w przyszłość) |
| [app/features/compute.py](../app/features/compute.py)* | features liczone point-in-time | look-ahead przez kuchenne drzwi |
| [app/db.py](../app/db.py) | schemat, `as_of_date`, rozdział danych demo/real | podrobiona historia w track recordzie |
| [app/paper/loop.py](../app/paper/loop.py) + [store.py](../app/paper/store.py) | księgowość paper (idempotencja, parity z backtestem) | podwójne fille, rozjazd paper vs backtest |
| [app/strategy/engine.py](../app/strategy/engine.py) | interpreter YAML (sygnały, nigdy pieniądze) | — |

\* `compute.py` to jedyny "core", który **będzie** edytowany — przy dodawaniu nowych features. Ale to zadanie dla Claude'a z testami point-in-time, nie ręczna edycja.

### Twoje pole zabaw (wszystko w `config/`, czysty YAML)

| Plik | Co kontroluje |
|---|---|
| `config/strategies/*.yaml` | **reguły wejścia/wyjścia + cały blok ryzyka** — tu żyją Twoje pomysły |
| `config/universe.yaml` | lista spółek (dodanie spółki GPW = jedna linijka + `make ingest`) |
| `config/news_sources.yaml` | feedy RSS ("dodanie feedu = edycja tego pliku, NIE kodu" — cytat z nagłówka) |
| `config/backtest.yaml` | kapitał, koszty, okna walk-forward, bramki walidacji, `user_id` |
| `config/llm.yaml` | modele, pinowanie providerów, budżet $ (twardy limit 10 USD/mies.) |
| `config/intraday.yaml`, `data_quality.yaml`, `backup.yaml` | recorder intraday, progi jakości danych, backupy |

### Siatka bezpieczeństwa (co Cię złapie, jak coś zepsujesz)

- **`make test`** — ~308 testów, w tym: anty-look-ahead, parity paper↔backtest co do bajta, 11 testów sizingu, testy fillów i timingu. Reguła projektu: czerwone testy = stop.
- **Hash configu** — po każdej zmianie strategii/kosztów/uniwersum pętla papierowa odmawia pracy, dopóki świadomie nie potwierdzisz.
- **Luka, o której musisz wiedzieć:** testy pilnują *logiki kodu*, nie *wartości* w YAML-ach. `risk_per_trade: 0.5` (50% na trade!) przejdzie testy i się uruchomi. YAML-e to Twoja odpowiedzialność — dlatego review na PR (niżej) ma sens.
- Druga luka: **nie ma CI** (GitHub Actions) — testy odpalają się tylko lokalnie. Warto dodać, zwłaszcza przy dwóch osobach.

## 6. "Wspólny excel" — jak pracować we dwóch

Repo już jest na GitHubie (`github.com/pantol/fin_opus`, historia PR-ów istnieje). Model pracy:

1. **Nie edytujemy na żywo tego samego pliku** (to nie Google Docs) — każdy robi branch → Pull Request → drugi patrzy na diff → merge. Zmiany YAML-i są małe i czytelne w review, idealne dla nie-programisty.
2. Dron może nawet nie klonować repo — edycja YAML-a i otwarcie PR-a działa **z poziomu strony GitHuba**.
3. **Docelowa "wersja udostępniana"** to nie wspólny plik, tylko wspólny *serwer*: jedna maszyna (VPS) odpala cronem `make signals` wieczorem, baza SQLite żyje tam, a każdy z Was dostaje swoje alerty na swój Telegram. Makefile ma już nawet gotową linijkę crona w komentarzu.
4. Dwie rzeczy, które warto dołożyć zanim dojdzie druga osoba: CI (testy na każdym PR) i ochrona brancha `main` (merge tylko przez PR).

## 7. Personalizacja: Twoje kakao pod El Niño, profil Drona

### Stan dzisiejszy — szczerze

- `user_id` jest na wszystkich tabelach pieniężnych (decyzje/pozycje/trade'y/equity), **ale** ma jedną zahardkodowaną wartość `default` ([config/backtest.yaml:5](../config/backtest.yaml)). Nie ma tabeli użytkowników, profili, watchlist ani pojęcia "portfela zewnętrznego".
- System **nie widzi nic spoza swojej papierowej książki** — Twój long na kakao u brokera nie ma się gdzie zapisać.
- Telegram to jeden globalny czat z env (`TELEGRAM_CHAT_ID`). Ciekawostka: funkcja wysyłki *już przyjmuje* `chat_id` per wywołanie ([app/alerts/telegram.py:39](../app/alerts/telegram.py)) — nikt z tego jeszcze nie korzysta. Szew na wielu użytkowników jest przygotowany.

### Minimalny projekt (zgodny z konwencjami repo): `config/users/<user>.yaml`

```yaml
# config/users/kamil.yaml
telegram_chat_id: "123456789"
strategy: trend_momentum          # każdy user może wskazać inną strategię
external_holdings:                # TYLKO informacyjnie — nigdy do matematyki pieniędzy
  - {asset: "Kakao (ICE Cocoa futures, long)", note: "teza El Niño, otwarte ręcznie"}
watch_themes:
  - name: "El Niño / kakao"
    keywords: [kakao, cocoa, "El Nino", ENSO, Ghana, "Wybrzeże Kości Słoniowej", susza]
watch_tickers: [OPL]
```

```yaml
# config/users/dron.yaml
telegram_chat_id: "987654321"
strategy: falling_knife           # jego własny YAML, jego własna książka
watch_themes: []                  # kakao mu się nie wyświetli — nie ma tematu w profilu
```

Do tego dwa poziomy personalizacji (można wdrożyć niezależnie):

1. **Poziom alertów** (tani, bez dotykania pieniędzy): filtr w miejscu, gdzie pętla wysyła karty na Telegram — każdy dostaje na swój `chat_id` tylko sygnały pasujące do profilu + swój "digest tematyczny". Zero wpływu na track record.
2. **Poziom książek** (pełna personalizacja): każdy user = własny `user_id` = **własny niezależny portfel papierowy** z własną strategią. Schemat bazy już to umie (książki bootstrapują się per `user_id`); do dopisania jest tylko pętla po profilach w CLI. Dron rozwija swój YAML nie ruszając Twojego — a hash configu pilnuje każdej książki osobno.

Jedna pułapka znaleziona przy okazji: monitor intraday czyta **wszystkie** otwarte pozycje bez filtra `user_id` ([app/alerts/monitor.py:53](../app/alerts/monitor.py)) — przy wielu użytkownikach wysyłałby wszystko wszystkim. Do poprawki razem z profilami.

### A samo kakao / El Niño?

Rozdzielmy dwie rzeczy:

- **Kakao w ścieżce pieniędzy (pozycje, sizing): nie teraz.** Cały system jest GPW/PLN-only: domyślne źródło danych filtruje po walucie PLN, nie ma obsługi FX, model kosztów jest pod polskiego brokera, benchmark to WIG20TR (reguła projektu). Surowce/zagranica to osobna, świadoma faza (jest w planach jako "later also emerging markets").
- **Kakao w ścieżce informacji (alerty, digest): jak najbardziej.** Dodajesz zagraniczne feedy do `news_sources.yaml` (schemat kolektora już przyjmuje newsy nieprzypisane do spółek GPW — zapisują się z pustym `instrument_id`), deterministyczny filtr słów kluczowych z Twojego profilu skleja z nich "digest tematyczny" i wysyła tylko na Twój czat. LLM może streszczać — ale zgodnie z regułą nr 1 nigdy nie decyduje o pieniądzach.
- **Uwaga praktyczna:** nawet ta "kompleksowa" lista z mythos_finance **nie ma ani jednego feedu rolno-surowcowego** (najbliżej jest ropa). Pod kakao/El Niño trzeba dobrać źródła osobno (np. ICCO, serwisy soft commodities, feedy pogodowe ENSO/NOAA).

## 8. Jak to ulepszyć — proponowana kolejność

Uporządkowane wg efekt/nakład, spójne z roadmapą już zapisaną w PROGRESS.md:

1. **Rozszerz uniwersum** — to jest #1 powód nudnych wyników i najtańsza zmiana (jedna linijka YAML na spółkę + `make ingest`). "Zakamarki rynku" na GPW to mWIG40/sWIG80, nie WIG20. Uwaga: zgodnie z regułą anty-survivorship trzeba też dopisywać spółki zdjęte z giełdy (masz do tego notatkę o archiwalnym API Bossa).
2. **Dodaj features pod nowe archetypy strategii** (RSI, obsunięcie od szczytu 52W, z-score wolumenu) — mała praca w `compute.py` z testami point-in-time. To odblokowuje mean-reversion, łapanie noży, breakouty.
3. **Napisz i przebacktestuj `falling_knife.yaml`** (szkic wyżej) — walk-forward, koszty realne, porównanie z WIG20TR. Silnik walidacji (Monte Carlo, Deflated Sharpe, rejestr prób) już jest gotowy i sam powie, czy strategia nie jest szumem.
4. **Wypełnij [docs/kill_criteria.md](kill_criteria.md) i wystartuj prawdziwy track record** — szablon kill-criteria jest w 100% pusty (same `____`), a to celowo *pre-rejestracja*: progi masz ustalić ZANIM zobaczysz wyniki. To też odblokowuje test A/B warstwy LLM, który dziś jest zablokowany brakiem realnych danych — a RSS nie ma backfillu, więc **każdy dzień bez włączonego kolektora to dzień danych stracony bezpowrotnie**.
5. **Profile użytkowników** (sekcja 7) — najpierw poziom alertów, potem osobne książki dla Ciebie i Drona.
6. **Faza 3 z roadmapy: radar reżimów / punkty zwrotne** — to jest dokładnie odpowiedź na Twoje "wycenia przeszłość": LLM czyta świat i produkuje *features* (np. reżim rynku), ale sygnał z nich liczy deterministyczny kod. Dopiero za tym: zagranica/surowce w ścieżce pieniędzy (FX, koszty, nowe źródła danych).

---

*Wygenerowano na sesji 2026-07-22; wszystkie odwołania do plików i linii zweryfikowane w kodzie na commit `160d01e`.*
