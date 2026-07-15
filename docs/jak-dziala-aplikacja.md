# Jak działa ta aplikacja? Przewodnik krok po kroku (bez żargonu)

**W jednym zdaniu:** to program, który każdego wieczoru po zamknięciu warszawskiej
giełdy (GPW) analizuje notowania, wybiera spółki spełniające ustalone z góry
reguły i wysyła na telefon gotowe podpowiedzi: *co kupić, ile sztuk i kiedy
sprzedać* — na razie wyłącznie na **wirtualnym portfelu**, bez ani jednej
prawdziwej złotówki.

---

## Trzy rzeczy, które trzeba wiedzieć na start

1. **Program nie składa prawdziwych zleceń.** Prowadzi tzw. portfel papierowy:
   startuje ze 100 000 wirtualnych złotych i „kupuje" oraz „sprzedaje" tylko na
   papierze, żeby uczciwie sprawdzić, czy strategia w ogóle działa.
2. **O pieniądzach decyduje wyłącznie kalkulator, nie sztuczna inteligencja.**
   Wszystkie decyzje — co, ile, po czym i z jakim zabezpieczeniem — wylicza
   zwykły, przewidywalny kod: te same dane zawsze dadzą tę samą decyzję.
   Sztuczna inteligencja (w przyszłych etapach) będzie mogła najwyżej
   *dostarczać informacje* (np. streszczać komunikaty spółek), ale nigdy nie
   dotknie kwot.
3. **Wszystko zostaje zapisane.** Każda decyzja ląduje w dzienniku razem z
   danymi, na podstawie których zapadła — po miesiącach można prześledzić
   każdy ruch i sprawdzić, czemu program zrobił to, co zrobił.

---

## Dzień z życia programu — krok po kroku

Program pracuje raz dziennie, wieczorem. W ciągu dnia giełda handluje, a on
czeka — decyzje podejmuje wyłącznie na podstawie cen z zamknięcia sesji.

### Krok 0 — w ciągu dnia: zbieranie komunikatów spółek

**Co się dzieje:** osobny, pomocniczy moduł cyklicznie zbiera oficjalne
komunikaty giełdowe spółek (tzw. ESPI/EBI) i nagłówki wiadomości, zapisując
przy każdym dokładną datę publikacji.

**Efekt:** rosnące archiwum informacji „kto co ogłosił i kiedy". Dziś służy
tylko jako magazyn — w przyszłości sięgnie po nie warstwa AI. Na dzisiejsze
decyzje o pieniądzach nie ma to żadnego wpływu.

### Krok 1 — ok. 19:00: giełda publikuje wyniki sesji

**Co się dzieje:** GPW udostępnia oficjalny plik z wynikami dnia: ceny
otwarcia i zamknięcia, najwyższą i najniższą oraz obrót (ile sztuk zmieniło
właściciela). O 19:30 program automatycznie pobiera te dane i dopisuje je do
swojej bazy. Baza celowo zawiera też spółki, które z giełdy już zniknęły —
dzięki temu testy historyczne nie oszukują, pokazując tylko „ocalałych".

**Efekt:** kompletna, lokalna historia notowań, zaktualizowana o dzisiejszą
sesję.

### Krok 2 — kontrola jakości: czy w ogóle wolno dziś decydować?

**Co się dzieje:** zanim program czegokolwiek dotknie, sprawdza dane jak
bramkarz na wejściu:

- Czy dane są świeże? (starsze niż 4 dni = STOP)
- Czy jest komplet spółek z dzisiejszej sesji? (mniej niż połowa = STOP —
  to znak, że pobieranie się nie udało)
- Czy od wczoraj nie zmieniły się reguły gry (strategia, koszty, lista
  spółek)? Taka zmiana wymaga świadomego potwierdzenia człowieka.
- Czy w bazie nie ma danych testowych zamiast prawdziwych?

**Efekt:** zielone światło — albo twarde STOP z alarmem ⚠️ na Telegramie
(np. *„Sygnaly GPW (paper): WSTRZYMANE — ingest broken?"*). Zasada jest
prosta: **lepiej nie podjąć decyzji, niż podjąć ją na złych danych**.

### Krok 3 — rozliczenie wczorajszych podpowiedzi

**Co się dzieje:** sygnały z wczorajszego wieczoru były „zleceniami
oczekującymi". Program realizuje je teraz po **dzisiejszej cenie otwarcia** —
tak jakby rano ktoś zaniósł je do biura maklerskiego. Liczy przy tym
wszystkie realne koszty:

- prowizję maklerską (0,38%, min. 3 zł),
- spread (kupuje się drożej, sprzedaje taniej — jak w kantorze),
- poślizg cenowy (duże zlecenie „przesuwa" cenę),
- limit płynności (nie można kupić więcej niż 10% dziennego obrotu — jeśli
  spółką mało kto handluje, zlecenie zrealizuje się częściowo albo wcale).

**Efekt:** potwierdzenia na Telegramie, np. *„✅ KUPIONO PKO: 178 szt. po
111,22 PLN"*, i zaktualizowany stan gotówki. Jeśli czegoś nie dało się
kupić/sprzedać — jasna informacja dlaczego.

### Krok 4 — wycena portfela i podnoszenie zabezpieczeń

**Co się dzieje:** program wycenia wszystkie posiadane akcje po dzisiejszych
cenach i przesuwa tzw. stopy kroczące. Stop to poziom alarmowy: „jeśli kurs
spadnie tutaj — sprzedajemy". Gdy kurs rośnie, stop jedzie w górę za nim
(zabezpiecza narastający zysk), ale **nigdy nie cofa się w dół**.

**Efekt:** aktualna wartość portfela oraz odświeżone poziomy obronne
(konkretny przykład — w sekcji „Symulacja" poniżej).

### Krok 5 — obliczenie wskaźników („karta zdrowia" każdej spółki)

**Co się dzieje:** dla każdej spółki program liczy kilka prostych miar,
wyłącznie z danych dostępnych do dziś (nigdy „z przyszłości"):

- **średnią z 200 sesji** — linia długoterminowego trendu,
- **momentum 6 miesięcy** — czy kurs jest wyżej niż pół roku temu,
- **ATR** — o ile spółka typowo waha się dziennie (miara nerwowości),
- pomocniczo: zmienność, siłę względem indeksu WIG.

**Efekt:** tabela „stanu zdrowia" wszystkich spółek na dzisiejsze zamknięcie.

### Krok 6 — reguły strategii: kto zasługuje na sygnał?

**Co się dzieje:** program przykłada do każdej spółki dwie proste reguły
(strategia „trend + momentum", zapisana w pliku konfiguracyjnym, nie w kodzie):

- **KUP**, gdy kurs jest **powyżej średniej 200-sesyjnej** ORAZ **wyżej niż
  pół roku temu** (spółka w potwierdzonym trendzie wzrostowym),
- **SPRZEDAJ**, gdy kurs spadnie do stopu kroczącego ALBO poniżej średniej
  200-sesyjnej (trend złamany).

Bez wyjątków, bez „przeczucia", bez dogrywek — reguły są bezduszne celowo.

**Efekt:** krótka lista surowych sygnałów: „te spółki kupić, tamte sprzedać".

### Krok 7 — warstwa ryzyka: ile kupić i czy w ogóle wolno

**Co się dzieje:** sygnał „kup" to dopiero połowa decyzji. Osobny moduł
wylicza wielkość pozycji tak, by pojedyncza wpadka bolała najwyżej odrobinę:

- na jednej transakcji można stracić najwyżej **1% portfela** (licząc od ceny
  wejścia do stopu),
- maksymalnie **20% portfela w jednej spółce** i **40% w jednej branży**,
- najwyżej **8 pozycji** naraz, żadnych zakupów na kredyt,
- **bezpiecznik awaryjny:** jeśli portfel jest ponad 25% pod kreską od
  szczytu, nowe zakupy są wstrzymane do odwołania.

**Efekt:** konkretne zlecenie — *„KUP PKO, 178 szt., stop 106,34 zł"* — albo
rezygnacja z sygnału, gdy limity na to nie pozwalają. Zlecenie trafia do
kolejki i zrealizuje się jutro na otwarciu (Krok 3 następnego dnia).

### Krok 8 — zapis do dziennika

**Co się dzieje:** każda decyzja zostaje zapisana razem z kompletem danych,
na których zapadła: wartości wskaźników, parametry strategii, data, godzina.
Zapis następuje **zanim** wyśle się jakiekolwiek powiadomienie — awaria
Telegrama nie może naruszyć księgowości.

**Efekt:** pełna, trwała historia — „czarna skrzynka" systemu.

### Krok 9 — powiadomienia na Telegram

**Co się dzieje:** na sam koniec program wysyła po polsku krótkie karty na
telefon (a bez skonfigurowanego Telegrama — wypisuje je na ekranie i
przechowuje do wysłania później).

**Efekt — dokładnie takie wiadomości:**

```text
📌 Sygnal GPW (paper)          ✅ Zlecenie GPW (paper): ZREALIZOWANO
Akcja: KUP PZU                 KUPIONO PKO: 178 szt. po 111.22 PLN
Ilosc: 279                     Data: 2026-07-14
Data decyzji: 2026-07-14
Stop: 65.58 PLN                📊 Portfel GPW (paper)
Realizacja: otwarcie           Sesja: 2026-07-14
nastepnej sesji                Kapital: 99,636.23 PLN
                               Otwarte pozycje: 6
```

---

## Co jest produktem końcowym?

**Codziennie wieczorem:** kilka czytelnych kart na telefonie — co system
proponuje kupić (z liczbą sztuk i poziomem obronnym), co właśnie „kupił" lub
„sprzedał" w portfelu papierowym i ile ten portfel jest wart.

**W dłuższej perspektywie:** uczciwy, niepodrabialny zapis wyników strategii
prowadzony dzień po dniu na prawdziwych cenach i z prawdziwymi kosztami.
Po kilku miesiącach odpowie na najważniejsze pytanie: **czy te reguły
naprawdę zarabiają lepiej niż zwykłe „kup indeks WIG20TR i trzymaj"?**
Jeśli nie — strategia idzie do kosza, a nie do prawdziwego rachunku.

---

## Symulacja: dwa prawdziwe wieczory, krok po kroku

Żeby sprawdzić cały cykl w praktyce, 15 lipca 2026 przeprowadziliśmy pełną
symulację na **kopii prawdziwych danych GPW** (sesje z 13 i 14 lipca).
Prawdziwe ceny, prawdziwe koszty, wirtualne pieniądze — a „prawdziwy"
portfel papierowy pozostał nietknięty i wystartuje osobno.

### Wieczór 1 — poniedziałek 13 lipca, 19:30

Program założył portfel: **100 000 zł** (wirtualnych). Rozliczać nie było
czego (portfel świeży), dane przeszły bramki jakości. Wskaźniki i reguły
wskazały 6 spółek w potwierdzonym trendzie wzrostowym, a warstwa ryzyka
wyliczyła dla każdej liczbę sztuk i poziom obronny:

| Sygnał | Ile sztuk | Stop (poziom obronny) |
|---|---:|---:|
| KUP PKO BP | 178 | 106,34 zł |
| KUP Pekao | 82 | 231,37 zł |
| KUP Orlen | 99 | 133,91 zł |
| KUP KGHM | 28 | 270,13 zł |
| KUP Alior | 1 | 134,41 zł |
| KUP Orange Polska | 815 | 13,37 zł |

Smaczek pokazujący limity ryzyka w praktyce: **dlaczego Alior dostał tylko
1 akcję?** Bo PKO i Pekao (też banki) zajęły już po ok. 20 tys. zł, niemal
wyczerpując 40-procentowy limit na jedną branżę — na trzeci bank zostało
dosłownie kilkadziesiąt złotych miejsca.

Na telefon poszło 7 wiadomości: 6 kart sygnałów + podsumowanie, np.:

```text
📌 Sygnal GPW (paper)
Akcja: KUP PKO
Ilosc: 178
Data decyzji: 2026-07-13
Stop: 106.34 PLN
Realizacja: otwarcie nastepnej sesji (potwierdzenie jutro)
```

**Ważne: tego wieczoru nic jeszcze nie kupiono.** Zlecenia czekają na
jutrzejsze otwarcie — dziś nikt nie zna jutrzejszych cen.

### Wieczór 2 — wtorek 14 lipca, 19:30

Rano giełda otworzyła sesję i wczorajsze zlecenia „zrealizowały się" po
cenach otwarcia — z prowizją, spreadem i poślizgiem:

| Kupiono | Ile sztuk | Cena zakupu | Prowizja |
|---|---:|---:|---:|
| PKO BP | 178 | 111,22 zł | 75,23 zł |
| Pekao | 82 | 239,48 zł | 74,62 zł |
| Orlen | 99 | 144,29 zł | 54,28 zł |
| KGHM | 28 | 308,92 zł | 32,87 zł |
| Alior | 1 | 143,19 zł | 3,00 zł |
| Orange Polska | 815 | 14,76 zł | 45,71 zł |

Ceny zakupu różnią się od poniedziałkowych — to normalne: zlecenie
realizuje się po **realnej cenie następnego otwarcia**, powiększonej o
spread i poślizg. Symulacja celowo niczego tu nie upiększa.

Po wycenie na wtorkowym zamknięciu portfel był wart **99 636,23 zł**
(−0,36%). Skąd ten minus pierwszego dnia? ~286 zł prowizji oraz to, że
kupuje się po cenie sprzedających, a wycenia po kursie zamknięcia. To
uczciwy koszt wejścia, który strategia musi dopiero odrobić.

Do tego: stopy kroczące od razu podjechały w górę za rosnącymi kursami
(KGHM: 270,13 → 287,59 zł; Orlen: 133,91 → 136,86 zł), a reguły znalazły
jeden nowy sygnał — **KUP PZU, 279 szt., stop 65,58 zł** — który czekał już
na środowe otwarcie. Na telefon poszło 8 wiadomości, m.in.:

```text
✅ Zlecenie GPW (paper): ZREALIZOWANO
KUPIONO PKO: 178 szt. po 111.22 PLN
Data: 2026-07-14

📊 Portfel GPW (paper)
Sesja: 2026-07-14
Kapital: 99,636.23 PLN (gotowka: 25,173.25 PLN)
Otwarte pozycje: 6
```

### Na koniec: próba awarii (celowo zepsuliśmy dane)

Przestawiliśmy systemowi zegar o tydzień do przodu, nie dając mu świeżych
notowań. Reakcja: **odmowa podjęcia jakiejkolwiek decyzji** i alarm na
telefon — dokładnie tak, jak powinno być:

```text
⚠️ Sygnaly GPW (paper): WSTRZYMANE
Powod: latest session 2026-07-14 is 8 days old (max_staleness_days=4) — ingest broken?
Szczegoly: make signals
```

Portfel pozostał nietknięty.

### Bilans symulacji

W dwa wieczory system przeszedł pełny cykl życia: **sygnał → zakup z
uczciwymi kosztami → ochrona rosnącego zysku (stopy) → nowy sygnał →
odporność na awarię danych**. Stan końcowy: 6 pozycji, 25 173,25 zł
gotówki, jedno zlecenie oczekujące (PZU) i komplet wpisów w dzienniku
decyzji.

---

## Skąd wiadomo, że reguły nie są wyssane z palca?

Zanim strategia w ogóle trafiła do codziennego użytku, przeszła symulację na
danych z wielu lat (tzw. backtest) — z kilkoma zabezpieczeniami przed
samooszukiwaniem:

- **Bez podglądania przyszłości:** każda decyzja w symulacji używa tylko
  informacji dostępnych w tamtym dniu.
- **Bez „efektu ocalałych":** w testach biorą udział także spółki, które
  później zbankrutowały lub zniknęły z giełdy.
- **Z pełnymi kosztami:** prowizje, spread, poślizg i limit płynności — tak
  jak w Kroku 3.
- **Na danych, których system „nie widział":** reguły stroi się na jednym
  okresie, a ocenia na następnym, przesuwając okno w przód (walk-forward).
- **Test na fart:** wynik porównuje się z tysiącem strategii „losowych" o
  identycznych kosztach oraz koryguje o liczbę prób. Strategia musi pobić
  i indeks, i przypadek — inaczej nie przechodzi.

---

## Czego ten program NIE robi

- ❌ Nie składa prawdziwych zleceń i nie ma dostępu do prawdziwych pieniędzy
  (to twarda zasada projektu, nie tymczasowe ustawienie).
- ❌ Nie gwarantuje zysków — sprawdza dopiero, czy strategia ma przewagę.
- ❌ Nie doradza indywidualnie — to narzędzie wspierające; decyzje (i
  odpowiedzialność) pozostają po stronie człowieka.
- ❌ Nie handluje w ciągu dnia — patrzy wyłącznie na ceny zamknięcia.
- ❌ Nie pozwala sztucznej inteligencji decydować o pieniądzach — AI może
  najwyżej podsuwać informacje, które i tak przechodzą przez warstwę ryzyka.

---

## Słowniczek

| Pojęcie | Po ludzku |
|---|---|
| Sesja | Jeden dzień handlu na giełdzie (9:00–17:00). |
| Kurs otwarcia / zamknięcia | Cena z początku / końca sesji. |
| Portfel papierowy (paper trading) | Inwestowanie „na niby" — prawdziwe ceny, wirtualne pieniądze. |
| Sygnał | Podpowiedź strategii: kup albo sprzedaj. |
| Stop (kroczący) | Poziom cenowy, przy którym pozycję się sprzedaje; rośnie za kursem, nigdy nie spada. |
| ATR | Typowy dzienny zakres wahań spółki — im wyższy, tym spółka „nerwowsza". |
| Spread | Różnica między ceną kupna a sprzedaży w danej chwili (jak w kantorze). |
| Poślizg | Pogorszenie ceny przy realizacji zlecenia, zwłaszcza dużego. |
| Obrót / płynność | Ile akcji zmienia właściciela — im mniej, tym trudniej kupić/sprzedać bez wpływu na cenę. |
| WIG20TR | Indeks 20 największych spółek GPW z reinwestowanymi dywidendami — punkt odniesienia („czy biję rynek?"). |
| Backtest | Symulacja strategii na danych historycznych. |
| Walk-forward | Uczciwa odmiana backtestu: ocena zawsze na danych spoza okresu strojenia. |

---

## Chcę to zobaczyć na własne oczy

- **Pełny raport techniczny z powyższej symulacji** (po angielsku):
  [docs/simulations/day-01-2026-07-15.md](simulations/day-01-2026-07-15.md)
- **Makieta powiadomień Telegram** (jak to wygląda na telefonie):
  [docs/simulations/day-01-2026-07-15-telegram-mockup.html](simulations/day-01-2026-07-15-telegram-mockup.html)
  — wystarczy otworzyć w przeglądarce.
- **Instrukcja uruchomienia** (instalacja, pierwsze kroki): [README.md](../README.md)
