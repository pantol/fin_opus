# System decyzyjny pod GPW — blueprint techniczny i roadmapa

> Wersja robocza. Ceny i darmowe limity są orientacyjne (stan ~czerwiec 2026) i zmieniają się szybko — zweryfikuj przed wdrożeniem. To dokument inżynierski, nie porada inwestycyjna ani prawna.

---

## 0. TL;DR — co naprawdę budujesz i w jakiej kolejności

To, co opisujesz, to **silnik filtrowania, oceny i porządkowania sygnałów**, w którym AI jest warstwą językową, a nie wyrocznią. Trzy zasady, które decydują o tym, czy projekt będzie „uczciwym narzędziem", czy „magic AI botem":

1. **Pieniądze liczy KOD, nie LLM.** LLM nigdy nie wylicza wielkości pozycji, ceny wejścia/wyjścia ani werdyktu „kup/sprzedaj" jako liczby. LLM produkuje *cechy* (sentyment, tagi, streszczenia), które kod traktuje jako *wejście* i bramkuje regułami. To jest Twoja jedyna realna ochrona przed halucynacją.
2. **Każda decyzja musi być odtwarzalna i zbacktestowalna.** Jeśli nie potrafisz odtworzyć, dlaczego system 3 miesiące temu dał sygnał, nie nadaje się do komercjalizacji.
3. **Każda nowa złożoność musi najpierw pobić benchmark na paper tradingu out-of-sample.** Inaczej tylko dokładasz koszt i ryzyko.

**Kolejność, która oszczędza pieniądze i nerwy** (rozwinięta w sekcji 16):
Dane → backtest JEDNEJ prostej reguły (zero LLM) → warstwa ryzyka + logowanie + Telegram → dopiero potem LLM jako cechy → potem radar/turning pointy → potem strategie akademickie → na końcu profil użytkownika i multi-tenant.

**Twarda prawda o „samouczącym się modelu":** w naiwnym sensie („AI uczy się na błędach i coraz lepiej przewiduje rynek") to praktycznie święty Graal i w 95% przypadków nie działa — prowadzi do przeuczenia na szumie. Wersja, która *działa*, to: rygorystyczne mierzenie + ostrożna re-kalibracja wag strategii + bezlitosna walidacja out-of-sample, z człowiekiem w pętli. Szczegóły w sekcji 13.

---

## 1. Filozofia: LLM vs warstwa algorytmiczna (cel + za/przeciw)

To była Twoja wprost zadana prośba, więc rozkładam ją na czynniki.

### Do czego służy każda warstwa

| | **Warstwa algorytmiczna (KOD)** | **LLM (agenci)** |
|---|---|---|
| **Cel** | Wszystko, co dotyczy liczb, pieniędzy i powtarzalności | Wszystko, co dotyczy języka → struktury i wyjaśnienia |
| **Konkretnie** | Cechy (momentum, zmienność, P/E, wolumen), scoring, sygnały regułowe, sizing pozycji, stopy, limity ekspozycji, egzekucja, backtest, ewaluacja, agregacja danych | Streszczanie raportów, ekstrakcja sentymentu/katalizatorów/ryzyk, klasyfikacja zdarzeń (np. „przejęcie", „emisja", „zmiana prognoz"), narracja werdyktu, dopasowanie semantyczne strategii do profilu, generowanie spersonalizowanego wyjaśnienia |

### LLM — za i przeciw

**Za:** rozumie nieustrukturyzowany tekst (raporty ESPI, newsy, prace PDF), świetny do ekstrakcji i klasyfikacji, generuje czytelne uzasadnienia (kluczowe dla „pracownika-analityka"), tani per-wywołanie przy modelach budżetowych, błyskawiczny w prototypowaniu.

**Przeciw:** **niedeterministyczny** (to samo wejście → różne wyjścia), **słaby w matematyce** (nie ufaj mu w żadnym wyliczeniu), **halucynuje** (wymyśla fakty/liczby), **trudny do zbacktestowania** (jak odtworzyć decyzję sprzed pół roku, skoro model się zmienił?), koszt rośnie liniowo z wolumenem, opóźnienie sieciowe, zależność od dostawcy.

### Warstwa algorytmiczna — za i przeciw

**Za:** deterministyczna i odtwarzalna, zbacktestowalna, audytowalna (regulator/klient policzy to samo), praktycznie darmowa przy skali, dokładna w liczbach.

**Przeciw:** „głupia" wobec tekstu i niuansu, wymaga ręcznego zaprogramowania reguł, sztywna (nie generalizuje sama z siebie).

### Żelazna reguła łączenia

```
TEKST  ──(LLM: ekstrakcja + tagi + score 0–1)──►  LICZBA/TAG  ──(KOD: walidacja, bramki, sizing)──►  DECYZJA
```

LLM zwraca **ustrukturyzowany JSON** (np. `{"sentiment": -0.6, "catalyst": "guidance_cut", "confidence": 0.8}`), a kod sprawdza zakresy, łączy z cechami quant i podejmuje decyzję. Jeśli LLM zwróci coś poza schematem — kod odrzuca i loguje, a nie „zgaduje dalej".

---

## 2. Architektura docelowa (Twoja, doprecyzowana)

Twój pipeline jest dobry. Dorzucam tylko dwie rzeczy: **bramkę determinizmu** (LLM → cecha → kod) i **warstwę regime** (radar) jako globalny filtr nad pojedynczymi sygnałami.

```
              ┌──────────────────────────────────────┐
              │  REGIME / RADAR  (KOD + LLM-narracja) │  globalny filtr risk-on/off
              └───────────────────┬──────────────────┘
                                  │ (gdy risk-off → tnij ekspozycję / blokuj longi)
   ┌─────────────┐                ▼
   │  Ingestion  │  ceny • newsy • raporty • makro  (point-in-time, wszystko z timestampem!)
   └──────┬──────┘
     ┌────┴───────────────────────┐
     ▼                            ▼
┌──────────────┐         ┌──────────────────┐
│ Research LLM │         │  Quant (KOD)     │
│ sentyment,   │         │  momentum, vol,  │
│ katalizatory │         │  P/E, wolumen    │
└──────┬───────┘         └────────┬─────────┘
       └───────────┬──────────────┘
                   ▼
        ┌─────────────────────┐
        │ Debata Bull/Bear LLM│  (opcjonalnie — patrz sekcja 7)
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │  Judge / Synteza LLM│  werdykt + konwikcja(0–1) + uzasadnienie → JSON
        └──────────┬──────────┘
                   ▼   ◄── BRAMKA DETERMINIZMU: kod waliduje JSON, łączy ze scoringiem quant
        ┌─────────────────────┐
        │ Warstwa ryzyka (KOD)│  sizing, stop, max ekspozycja, drawdown circuit-breaker
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │ Egzekucja (PAPER)   │  realistyczny fill + prowizja GPW + spread + poślizg
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │ Log + Ewaluacja     │  loguj WSZYSTKO → vs WIG/WIG20 (nie SPY — patrz niżej)
        └──────────┬──────────┘
                   ▼
        ┌─────────────────────┐
        │ Telegram alert      │  karta sygnału + uzasadnienie
        └─────────────────────┘
```

**Uwaga o benchmarku:** dla GPW benchmarkiem jest **WIG / WIG20 / WIG20TR (total return)**, nie SPY. Jeśli porównujesz do SPY, „wygrywasz" tylko dlatego, że łapiesz polski rynek/walutę. Dla rynków zagranicznych dobieraj benchmark do uniwersum (np. MSCI EM dla wschodzących).

---

## 3. Stack i koszty — drabina „darmowe → tanie → warte dopłaty"

Filozofia: **monolit + cron + jeden serwer**, zaprojektowany z czystymi szwami, żeby później dało się rozbić na usługi. Nie buduj mikroserwisów ani Kubernetesa na starcie — to dokładnie ten „przedwczesny koszt", którego chcesz uniknąć.

| Warstwa | Darmowe / start | Tania dopłata (warta ceny) | Kiedy dopłacać |
|---|---|---|---|
| **Język** | Python | — | od razu |
| **Orkiestracja agentów** | zwykły Python (funkcje + kolejka) | LangGraph (darmowy OSS) gdy pipeline się komplikuje | gdy masz >3 agentów z rozgałęzieniami |
| **LLM** | Gemini 2.5 Flash-Lite (~$0.10/$0.40 za 1M tok., free tier do prototypu); DeepSeek V3.2 (~$0.14/$0.28, OpenAI-kompatybilny, cache do ~50× taniej) | Gemini 2.5 Flash / Claude Haiku do trudniejszej syntezy; mid-tier tylko dla „Judge" | gdy tania synteza zawodzi na trudnych przypadkach |
| **Baza danych** | SQLite (lokalnie) | Postgres + TimescaleDB (np. Supabase free / Neon free) | gdy >1 użytkownik lub serie czasowe rosną |
| **Hosting / scheduler** | własny PC + cron; albo darmowy tier (Fly.io/Railway małe) | mały VPS ~$5/mc (Hetzner/Mikr.us) | gdy potrzebny 24/7 uptime |
| **Kolejka zadań** | cron + skrypty | Redis + RQ/Celery (Redis ~darmowy/tani) | przy realtime i wielu użytkownikach |
| **Object storage (PDFy, raporty)** | dysk lokalny | S3-kompatybilne (Cloudflare R2 — darmowy egress) | gdy biblioteka strategii rośnie |
| **Alerty** | Telegram Bot API (darmowe) | — | — |
| **Dashboard** | Streamlit (darmowy) | — | gdy chcesz podglądać wyniki wizualnie |

**Realny koszt fazy osobistej (tylko Ty, paper trading):** ~**0–15 USD/mc** (głównie tani LLM + ewentualnie VPS).
**Koszt LLM — pattern oszczędzania:** routuj 70–90% wywołań (klasyfikacja, ekstrakcja) do najtańszego modelu, eskaluj do droższego tylko trudną syntezę. Włącz **prompt caching** (ten sam system-prompt wysyłany w kółko = nawet kilkadziesiąt razy taniej na inpucie). Cache'uj też wyniki LLM po hashu wejścia — ten sam raport nie powinien być streszczany dwa razy.

---

## 4. Warstwa danych (Ingestion) — najtrudniejszy i najważniejszy element

To tu wygrywasz albo przegrywasz. **90% „cudownych" backtestów to artefakty złych danych.** Trzy grzechy główne:

- **Look-ahead bias** — używasz danych, których w danym momencie jeszcze nie było (np. dane finansowe „za Q1" w dniu, gdy raport jeszcze nie wyszedł). **Lekarstwo: timestampuj WSZYSTKO momentem publikacji, nie momentem, którego dotyczy.**
- **Survivorship bias** — backtestujesz tylko spółki, które dziś istnieją; pomijasz te zdelistowane/zbankrutowane. Na GPW to realny problem (delistingi). **Lekarstwo: trzymaj też martwe tickery.**
- **Restated fundamentals** — dane finansowe są później korygowane; backtest na skorygowanych = oszustwo.

### Źródła pod GPW (od darmowych)

| Typ danych | Darmowe | Uwagi / pułapki | Dopłata |
|---|---|---|---|
| **Ceny EOD (dzienne OHLCV)** | **Stooq** — pobieranie CSV (`stooq.pl/q/d/l/?s=TICKER&i=d`), pełna historia za darmo; archiwum **GPW** (`gpw.pl/archiwum-notowan`, XLS/CSV) | Stooq **nie ma oficjalnego API**, intraday opóźniony ~15–30 min, ale EOD jest solidny; uważaj na korekty o dywidendy/prawa poboru | dostawca danych płatny (np. EODHD) gdy chcesz pewności i więcej rynków |
| **Ceny intraday / „prawie live"** | **API brokera**: XTB xAPI (xStation, konto demo za darmo) lub **bossaAPI** (DM BOŚ) | demo XTB bywa „glitchy"; to wystarczy do paper tradingu, nie do HFT | dane realtime od dystrybutora = drogie, na start zbędne |
| **Fundamenty (P/E, P/B, wyniki kwartalne)** | biznesradar.pl, stockwatch.pl, money.pl | **to jest realna luka** — darmowe dane są rozproszone, częściowo płatne, ciężkie do point-in-time | abonament biznesradar/stockwatch lub EODHD fundamentals — pierwsza sensowna dopłata |
| **Raporty spółek ESPI/EBI** | **GPW** (`gpw.pl/komunikaty`), **PAP** (`espiebi.pap.pl`), bankier.pl (komunikaty) | to jest „pierwszy ślad" zdarzeń istotnych — paliwo dla Research Agenta; **timestampuj momentem publikacji** | — |
| **Makro PL** | **NBP API** (`api.nbp.pl` — FX, złoto, stopy; bez klucza, darmowe) | stabilne, świetne | — |
| **Makro globalne / risk-off** | **FRED** (St. Louis Fed, darmowy klucz — rentowności, spready kredytowe, krzywa); **ECB Data Portal**; **Eurostat**; **yfinance** (indeksy, VIX, VSTOXX, ETF-y — nieoficjalne, darmowe) | yfinance bywa zawodne — traktuj jako „nice to have", nie fundament | płatny feed makro tylko przy komercjalizacji |

> **Intel rynkowy:** istnieje już `espiai.pl`, który robi dokładnie „AI filtruje ESPI wg Twojego promptu" + alerty. To jednocześnie **walidacja pomysłu** i **konkurencja** — Twoją przewagą musi być dopasowanie strategii do profilu + transparentny backtest, nie sam fakt streszczania ESPI.

> **Licencje danych:** scraping do użytku osobistego ≠ redystrybucja komercyjna. Zanim udostępnisz to komukolwiek, sprawdź regulaminy Stooq/GPW co do komercyjnego wykorzystania danych. To realne ryzyko prawne na etapie komercjalizacji.

---

## 5. Warstwa quant (KOD) — cechy i scoring

Czysty kod (pandas / numpy / `pandas-ta` lub `TA-Lib`). Liczy cechy point-in-time:

- **Momentum / trend:** zwroty 1/3/6/12M, odległość od SMA/EMA (50/200), nachylenie średniej, ADX.
- **Zmienność / ryzyko:** ATR, zmienność realizowana, max drawdown, beta do WIG.
- **Wolumen / przepływy:** OBV, Accumulation/Distribution, wolumen vs średnia (proxy „smart money" — patrz sekcja 9).
- **Wartość / jakość (jeśli masz fundamenty):** P/E, P/B, EV/EBITDA, ROE, dług/EBITDA.
- **Relatywna siła:** ranking spółki vs sektor i vs WIG (to napędza rotację sektorową).

**Scoring:** każda cecha → znormalizowany score (z-score lub ranking percentylowy w uniwersum), potem ważona suma → `quant_score ∈ [-1, 1]`. Wagi są **parametrem strategii** (różne dla różnych strategii akademickich i profili).

---

## 6. Agenci LLM — implementacja z kontrolą halucynacji

### Research Agent
Wejście: news/raporty dla tickera + okno czasowe (point-in-time!). Wyjście — **wymuszony JSON schema**:
```json
{
  "sentiment": -1.0..1.0,
  "catalysts": ["string"],
  "risks": ["string"],
  "event_type": "earnings|m&a|guidance|issuance|...|none",
  "confidence": 0.0..1.0,
  "evidence_quote": "krótki cytat ze źródła"
}
```
`evidence_quote` zmusza model do zakotwiczenia w tekście (redukuje konfabulację). Kod sprawdza, czy cytat faktycznie występuje w źródle — jeśli nie, obniża zaufanie.

### Debata Bull/Bear + Judge (sekcja 7)
Opcjonalna. Bull dostaje cechy + research i argumentuje za longiem, Bear za shortem/unikaniem, Judge syntezuje. **Za:** mniej jednostronnych werdyktów, lepsze uzasadnienia. **Przeciw:** 3× koszt LLM, 3× opóźnienie, łatwo o „teatr" bez wartości dodanej. **Rekomendacja:** wdróż dopiero, gdy zmierzysz, że pojedynczy Judge daje gorsze wyniki — nie na start.

### Judge / Synteza
Łączy research + `quant_score` → JSON: `{"verdict": "bullish|neutral|bearish", "conviction": 0..1, "rationale": "..."}`. **To wszystko jest tylko WEJŚCIEM do warstwy ryzyka — nie decyzją o pieniądzach.**

### Kontrola kosztów i halucynacji (lista kontrolna)
- Wymuszaj `response_format` / JSON schema (większość dostawców to wspiera).
- Prompt caching na system-prompcie; cache wyników po hashu wejścia.
- Temperatura niska (0–0.3) dla powtarzalności.
- Loguj **pełny prompt, model, wersję, wyjście** przy każdej decyzji (audyt + przyszłe re-ewaluacje).
- Routing modeli: tani do ekstrakcji/klasyfikacji, droższy tylko do Judge.

---

## 7. Warstwa ryzyka + paper trading + backtest (deterministyczne serce)

To jest miejsce, gdzie „kwestia finansowa zostaje zaopiekowana w warstwie algorytmicznej", o co prosiłeś. **Zero LLM tutaj.**

### Warstwa ryzyka (KOD)
Wejście: werdykt + konwikcja (z LLM), `quant_score`, stan portfela, reguły profilu. Wyjście: konkretne zlecenie lub jego brak.
- **Sizing:** fixed-fractional (np. ryzyko 0.5–2% kapitału na trade), albo volatility-targeting (większa pozycja przy niższej zmienności), albo Kelly **z czapką** (pełny Kelly to samobójstwo — używaj ¼ Kelly max).
- **Stop loss:** oparty na ATR (np. 2–3× ATR) lub poziom techniczny.
- **Limity ekspozycji:** max % na pojedynczą spółkę, na sektor, na cały rynek; max liczba otwartych pozycji.
- **Circuit breaker:** przy drawdownie portfela > X% — zatrzymaj nowe pozycje (to też część „radaru").
- **Filtr regime:** gdy radar = risk-off → blokuj longi / tnij sizing (sekcja 9).

### Paper trading engine (KOD)
**Realistyczny fill** to różnica między uczciwym a oszukańczym systemem:
- prowizja GPW (modeluj realne stawki brokera),
- **spread** (kup po ask, sprzedaj po bid),
- **poślizg** (slippage) zależny od płynności — na GPW małe spółki są niepłynne, to zabija wiele strategii,
- brak fillów na zlecenia większe niż realny wolumen.

### Backtest (KOD)
- Biblioteki: `vectorbt` (szybki, wektorowy), `backtrader` lub własny event-driven.
- **Walk-forward / out-of-sample obowiązkowo** — optymalizuj na oknie, testuj na następnym, przesuwaj. In-sample wyniki nie znaczą nic.
- Metryki vs benchmark (WIG/WIG20TR): CAGR, Sharpe, Sortino, max drawdown, Calmar, win rate, profit factor, turnover (koszty!).
- **Uwaga na overfitting:** im więcej parametrów dostroisz, tym piękniejszy backtest i tym gorszy realny wynik. Mniej parametrów = lepiej.

---

## 8. AI radar kryzysowy + wykrywacz turning pointów — uczciwa wersja

Tu jest najwięcej pokusy, żeby wpaść w „magic bota". **Prawda:** to są problemy **detekcji reżimu** i **indykatorów złożonych**, które *zmniejszają* ryzyko i dają *wcześniejsze ostrzeżenie*, ale **nie przewidują przyszłości** i **dają fałszywe alarmy**. Sprzedawaj je jako „system wczesnego ostrzegania", nigdy jako „przewidywanie crashu".

### Radar kryzysowy / risk-off (KOD liczy, LLM narracja)
Composite z sygnałów, każdy zbacktestowany osobno:
- **Breadth:** % spółek powyżej SMA200, advance/decline, nowe maxy vs nowe minima.
- **Zmienność:** VIX/VSTOXX, zmienność realizowana, skok zmienności.
- **Krzywa rentowności** (inwersja — z FRED), **spready kredytowe** (rozszerzanie = stres).
- **Cross-asset risk-off:** ucieczka do USD/złota/obligacji, korelacje rosnące do 1.
- **Reżim trendu:** WIG poniżej SMA200 + nachylenie w dół.

Złóż w `risk_score ∈ [0,1]`; progi → `risk-on / neutral / risk-off`. LLM tylko **opisuje** stan po ludzku w alercie („risk-off: inwersja krzywej + spadająca szerokość rynku + skok VSTOXX"). **Zbacktestuj reguły alertów** — ile fałszywych alarmów, ile prawdziwych ostrzeżeń, jakie wyprzedzenie.

### Turning pointy (KOD)
- **Koniec bessy / początek hossy:** przebicie SMA200 z potwierdzeniem breadth + wolumenu; MACD/momentum na indeksie; trudne i opóźnione — uczciwie komunikuj opóźnienie.
- **Rotacja sektorowa:** ranking relatywnej siły sektorów (sekcja 5) + jego zmiany w czasie → „kapitał wchodzi do X, wychodzi z Y".
- **Panic sell-off:** ekstremalny wolumen + gwałtowny spadek + skok zmienności + szerokość rynku w dół.
- **„Smart money accumulation":** to **proxy**, nie magia — Accumulation/Distribution rosnące przy płaskiej/spadającej cenie, OBV rozjeżdżający się z ceną, nietypowy wolumen. **Nie nazywaj tego pewnością** — to hipoteza do potwierdzenia.

> Każdy z tych sygnałów musi przejść tę samą bramkę co strategie: backtest out-of-sample, raport fałszywych alarmów. Inaczej to wróżenie z fusów.

---

## 9. Pipeline strategii akademickich (PDF → spec → walidacja → biblioteka)

Cel: docelowo ~100 strategii z prac doktorów/profesorów, dopasowywanych do profilu. **Najtrudniejsza prawda:** większość opublikowanych strategii **nie przeżywa out-of-sample**, **zanika po publikacji** (rynek się adaptuje), ma **bias data-miningu**, i **nie była testowana na GPW** (mały, mniej płynny, inna mikrostruktura niż US/rynki rozwinięte). Dlatego pipeline = **maszyna do kodyfikacji i bezlitosnej walidacji**, nie do ślepego wdrażania.

```
PDF pracy
   │
   ▼
[LLM: ekstrakcja] ──► reguły strategii w języku naturalnym + parametry + uniwersum + horyzont
   │
   ▼
[CZŁOWIEK weryfikuje] ◄── KRYTYCZNE: LLM często źle czyta wzory/warunki; nie ufaj bez kontroli
   │
   ▼
[KOD: tłumaczenie na spec/DSL] ──► parametryzowany, zbacktestowalny config (YAML/JSON)
   │
   ▼
[KOD: walidacja] ──► backtest out-of-sample na GPW + walk-forward; ta sama bramka co wszystko inne
   │
   ▼
   ├── PRZECHODZI → do biblioteki strategii (z metrykami, tagami ryzyka, reżimami, w których działa)
   └── NIE PRZECHODZI → archiwum (loguj, czemu odpadła)
```

- **Reprezentacja strategii** jako config (DSL), nie kod per strategia — wtedy 100 strategii to 100 plików konfiguracyjnych uruchamianych przez jeden silnik. To jest też fundament skalowalności i komercjalizacji.
- **LLM pomaga czytać i kodyfikować; człowiek zatwierdza; kod waliduje.** Pełna automatyzacja PDF→strategia bez człowieka = generator śmieci.
- Każda strategia w bibliotece ma metadane: w jakim reżimie działa, jaki horyzont, jaka zmienność, jaki profil ryzyka, jakie wyniki OOS na GPW.

---

## 10. Profil inwestora / ankieta → dopasowanie (Twoja przewaga)

Twoja deklarowana przewaga to „prompt dopasowany do użytkownika". To dobre, ale **bramkowanie musi być deterministyczne** (kod), a nie zależeć od LLM — i dla bezpieczeństwa, i dla regulatora.

**Ankieta** mapuje na profil mierzący osobno:
- **Zdolność do ryzyka** (kapitał, horyzont, płynność, dochód) — obiektywna.
- **Tolerancja ryzyka** (psychologiczna, reakcja na drawdown).
- **Wiedza / rozumienie rynku** (dopuszczasz złożone strategie tylko świadomym).
- **Cele** (wzrost / dochód / ochrona kapitału).

```
Ankieta ──(KOD: scoring)──► Profil ──(KOD: reguły)──► zbiór dozwolonych strategii + parametry ryzyka
                                                          │
                                                          ▼
                                          (LLM: dopasowanie semantyczne + spersonalizowane wyjaśnienie)
```

KOD decyduje, **jakie** strategie i jakie ryzyko użytkownik w ogóle może dostać (np. początkujący nie dostaje dźwigni ani niepłynnych spółek). LLM dobiera ranking w ramach dozwolonego zbioru i generuje wyjaśnienie „dlaczego ta strategia pasuje do Ciebie". To realizuje Twój cel — „każdy inwestuje z czystym sumieniem zgodnie ze swoją wiedzą i awersją" — bez wpuszczania halucynacji do warstwy bezpieczeństwa.

---

## 11. Telegram — alerty

Najprostszy element. BotFather → token → `python-telegram-bot` lub zwykłe wywołania HTTP do Bot API (darmowe).
- **Karta sygnału:** ticker, kierunek, konwikcja, sizing, stop, krótkie uzasadnienie (z LLM), link do szczegółów.
- **Alerty radaru:** zmiana reżimu (risk-on→off), turning point, panic sell-off.
- Per-użytkownik: każdy ma swój `chat_id` i dostaje tylko sygnały zgodne ze swoim profilem.

---

## 12. Pętla „uczenia się" — uczciwa wersja (Twój deklarowany główny atut)

Tu jest największe pole do samooszukiwania. Rozdzielmy „uczenie się", które działa, od tego, które rujnuje.

**Co działa (rób to):**
1. **Loguj WSZYSTKO** — każdą decyzję, cechy, kontekst, prompt LLM, wynik, reżim. To fundament; bez tego „uczenie" jest niemożliwe.
2. **Atrybucja wyników** — która strategia/sektor/reżim zarabia, a która traci. Per strategia, per reżim rynkowy.
3. **Re-ewaluacja offline** — okresowo (np. miesięcznie) licz, co działało, na danych do danego momentu.
4. **Ostrożna re-alokacja wag (meta-allocation)** — przeważaj strategie, które działają w bieżącym reżimie, niedoważaj słabe. **To jest legalne „uczenie się".** Ale: walk-forward, czapki na zmiany wag, człowiek zatwierdza większe zmiany.
5. **Post-mortemy z LLM** — model *opisuje* (narracja), co poszło nie tak. Nie *optymalizuje* parametrów.

**Co rujnuje (NIE rób tego):**
- **Auto-optymalizacja na żywych wynikach** → przeuczenie na szumie, gonienie za ostatnim reżimem.
- **Pozwalanie systemowi po cichu przepisywać własne reguły handlowe** → katastrofa bez audytu.
- **Sprzężenia z LLM** „popraw się na podstawie strat" bez walidacji OOS → halucynacyjna spirala.

**Złota zasada:** każda zmiana, która dotyka pieniędzy, przechodzi przez walk-forward OOS i bramkę człowieka. „Uczenie się" = dyscyplina pomiaru + konserwatywna adaptacja, nie autonomiczna ewolucja.

---

## 13. Skalowanie i komercjalizacja (bez przepalania na starcie)

Zaprojektuj **szwy** teraz, ale **nie buduj** infrastruktury skali, dopóki nie masz użytkowników. Konkretnie:

- **Multi-tenant od modelu danych:** `user_id` w każdej tabeli decyzji/portfela/profilu od pierwszego dnia. Dodanie tego później jest bolesne; dodanie teraz jest darmowe.
- **Rozdziel rdzeń deterministyczny (współdzielony) od konfiguracji per-user** (profil, dozwolone strategie, parametry ryzyka, prompt). Rdzeń liczy raz, użytkownicy konsumują przez swoje filtry.
- **Bezstanowe workery + kolejka zadań** — gdy przyjdzie czas, łatwo zrównoleglić.
- **Postgres + TimescaleDB** na serie czasowe, **object storage (R2)** na PDFy/raporty.
- Strategie jako **configi (DSL)**, nie kod — wtedy „każdy implementuje swoją strategię" = dodaje config, nie redeploy.

Ale na fazę osobistą: **SQLite + cron + jeden skrypt** wystarczą. Przejście monolit→usługi rób, gdy realnie zabraknie wydajności, nie „na zapas".

---

## 14. Aspekty prawne i regulacyjne (przeczytaj zanim udostępnisz komukolwiek)

To nie porada prawna — to flaga, że potrzebujesz polskiego prawnika **przed** komercjalizacją.

- **Użytek osobisty (Ty, paper trading, własne środki):** zasadniczo brak problemu regulacyjnego.
- **Dawanie sygnałów/rekomendacji innym osobom:** prawdopodobnie wchodzisz w obszar regulowany — **rekomendacje inwestycyjne** (MAR), **doradztwo inwestycyjne** (MiFID II), nadzór **KNF**. Może wymagać licencji lub starannego ustrukturyzowania jako „narzędzie edukacyjne / nie-doradztwo" z mocnymi zastrzeżeniami. **To jest decyzja, której nie podejmuj bez prawnika.**
- **Licencje danych:** redystrybucja danych GPW/Stooq komercyjnie ≠ użytek osobisty. Sprawdź regulaminy.
- **„Nie scam magic bot" = radykalna transparentność:** publikuj uczciwe wyniki backtestów (z OOS, kosztami, drawdownami), **żadnych gwarancji zysku**, jasne disclaimery o ryzyku, pokazuj też przegrane. To jest dokładnie to, co odróżni Cię od scamów — i jest spójne z Twoim „czystym sumieniem".

---

## 15. Roadmapa fazowa — actionable, z bramkami

**Każda faza musi pobić benchmark na paper tradingu OOS, zanim przejdziesz dalej.**

| Faza | Co budujesz | Bramka wyjścia | Koszt |
|---|---|---|---|
| **0. Dane + 1 reguła** | Ingestion EOD (Stooq/GPW), point-in-time, backtest JEDNEJ prostej reguły (np. momentum + SMA200), **zero LLM** | Backtest OOS działa i jest wiarygodny (realne koszty/spread) | ~0 |
| **1. Ryzyko + log + Telegram** | Warstwa ryzyka (sizing, stop, limity), paper engine z realnym fillem, logowanie wszystkiego, alert na Telegram | Paper trading 1 strategii vs WIG; pełny log decyzji | ~0–5/mc |
| **2. LLM jako cechy** | Research Agent + Judge → JSON → bramka determinizmu; caching; routing modeli | LLM-cechy mierzalnie poprawiają wynik vs faza 1 (albo wytnij) | ~5–15/mc |
| **3. Radar + turning pointy** | Composite risk-score, filtr regime nad sygnałami, alerty radaru; backtest reguł alertów | Mniej drawdownu / lepszy Sharpe z filtrem regime; raport fałszywych alarmów | ~5–15/mc |
| **4. Biblioteka strategii** | Pipeline PDF→spec→walidacja; DSL strategii; 3–5 strategii akademickich przez bramkę OOS | ≥1 strategia akademicka przechodzi walidację na GPW | ~10–20/mc |
| **5. Profil + dopasowanie** | Ankieta → profil → reguły bramkujące → dopasowanie + spersonalizowany prompt | Działa dla 2–3 fikcyjnych profili end-to-end | ~10–20/mc |
| **6. Multi-tenant / komercjalizacja** | Postgres+Timescale, kolejka, `user_id` wszędzie, dashboard, **+ prawnik** | Drugi realny użytkownik działa bezpiecznie i rozdzielnie | rośnie |

---

## 16. Pierwsze dwa tygodnie — konkretnie

1. Środowisko: Python, repo Git, `requirements.txt` (pandas, numpy, requests, pandas-ta, vectorbt, python-telegram-bot, sqlite3).
2. Ściągnij EOD dla WIG20 + WIG (Stooq CSV), zapisz do SQLite z kolumną `as_of_date` = data publikacji. Dorzuć 5–10 zdelistowanych tickerów (anty-survivorship).
3. Policz cechy quant (momentum, SMA200, ATR) w czystym kodzie.
4. Napisz JEDNĄ regułę (np. long gdy cena > SMA200 i momentum 6M dodatnie; wyjście na stopie ATR).
5. Zbacktestuj **z prowizją + spreadem + poślizgiem**, walk-forward, porównaj do WIG20TR. Jeśli „wygrywa" za łatwo — szukaj błędu (look-ahead!).
6. Dodaj logowanie każdej decyzji do SQLite i wyślij testowy alert na Telegram.
7. Dopiero gdy to działa i jest wiarygodne — wepnij pierwszy LLM (Gemini Flash-Lite / DeepSeek) do streszczania ESPI jako *cechy*, z JSON schema i cache'em.

Gdy faza 0–1 stoi i wierzysz w swoje backtesty, reszta to dokładanie klocków na solidnym fundamencie — a nie budowanie magii na piasku.
