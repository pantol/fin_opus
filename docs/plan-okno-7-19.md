# PLAN: system pracujący w oknie 7:00–19:00

> Spisany 2026-07-23. Status: **do akceptacji** — nic z tego planu nie jest
> jeszcze zbudowane. Kontekst: prawie wszystkie klocki już istnieją
> (rejestrator intraday, monitor stopów, kolektor ESPI/newsów, pipeline LLM,
> harness A/B). Systemowi brakuje przede wszystkim **zegara**: nic nie jest
> zaplanowane, kolektor stoi, LLM ma 0 przebiegów, a Telegram nie ma tokena.

## Cel

Program działający w zdefiniowanych godzinach (np. 7:00–19:00), który na
bieżąco korzysta z cen akcji, informacji z rynku, analiz ze źródeł
zewnętrznych oraz analiz agenta LLM — i na tej podstawie wysyła alerty
decyzyjne i informacyjne na Telegramie.

## Zasady brzegowe (nienaruszalne)

- **Decyzje podejmuje wyłącznie deterministyczny kod.** LLM produkuje cechy
  (`llm_features`) i teksty informacyjne — nigdy nie decyduje o pieniądzach.
- **Decyzje intraday dopiero po intraday-backteście** (Etap 4). Do tego czasu
  wszystko w ciągu dnia to tier **informacyjny** — tak jak dziś monitor stopów.
- Paper only; „decyzja na bieżąco" = karta w Telegramie + zapis w księdze
  paper, nigdy realne zlecenie.
- Point-in-time wszędzie — `prices_intraday` już ma `as_of_ts`, newsy mają
  `published_at`.

## Docelowy dzień pracy (konfigurowalny w `config/schedule.yaml`)

| Godzina | Zadanie | Tier |
|---|---|---|
| 07:05 | `collect` — nocne ESPI/newsy | dane |
| 07:30 | **Digest poranny**: pozycje vs stopy, nowe komunikaty (+ skróty LLM), zaplanowane wydarzenia | info |
| 09:00–17:10 co 5 min | rejestrator intraday + monitor stopów (istnieje) | info |
| co 15 min | `collect` — newsy/ESPI w trakcie sesji | dane |
| co 30–60 min | LLM-events: ocena nowych newsów pod kątem pozycji/watchlisty | info |
| 17:30 / 17:45 | ostatni `collect` → `make llm` (materializacja cech na dziś) | dane |
| 18:15 | `ingest` → `signals` — **jedyne miejsce decyzji** (dane finalne po 18:00) | **decyzje** |
| 19:00 | healthcheck + karta statusu + backup | ops |

## Etapy (każdy z bramką `make test` / backtest)

### Etap 1 — Zegar i kanał (fundament, ~1–2 dni)

Nowy `app/scheduler.py` + `make daemon`: jeden proces, który czyta
`config/schedule.yaml` (okno 7–19, dni robocze, kalendarz GPW) i odpala
istniejące joby w oknie; journal `schedule_runs` (idempotencja, widoczny w
`make status`); generalizacja dedupu alertów (`alert_log` z priorytetami i
quiet-hours w `config/alerts.yaml`, routing per `user_id` — gotowe pod
drugiego użytkownika). Do tego: konfiguracja tokena Telegrama (env), digest
poranny, launchd na Macu (uwaga: uśpiony laptop = pominięte joby; docelowo
VPS — joby są idempotentne, więc nadgonią). **Zero zmian w decyzjach.**

### Etap 2 — Szersze źródła + LLM jako radar informacyjny (~2–3 dni)

`config/news_sources.yaml` (kuratorowana lista RSS — inspiracja z
`mythos_finance`, ale wpięta tutaj), kalendarz makro; nowa tabela
`llm_event_features` (filing_id, instrument, sentiment, relevance,
event_type, model+provider+generation_id, `as_of_ts`). W ciągu dnia karta
typu: *„⚠️ ESPI: CCC — profit warning. Ocena LLM: istotność 0.9, sentyment
−0.7. Pozycja otwarta, stop 12.40. (informacyjne — decyzja wieczorem)"*.
Reguły skilla `llm-provider-routing`: tani model do ekstrakcji, cache po
hashu, pinned provider, dzienny limit kosztów, walidowany JSON. Bramka:
precyzja na złotym zbiorze (`eval_labels` już istnieje) + koszt/dzień z
`llm_costs`.

### Etap 3 — LLM wchodzi do decyzji wieczornych (~1 dzień + czas na dowody)

`make llm` w harmonogramie przed `signals`; A/B OOS istniejącym harnessem
(`make ab`): `trend_momentum` vs `trend_momentum_llm`. Zamiast łamać
ciągłość świeżo wystartowanej księgi — **druga równoległa księga paper**
(`user_id = paper:llm`) na strategii z LLM: uczciwe porównanie na żywo, bez
`--accept-config-change` na głównej. Bramka: przewaga OOS vs WIG20TR.

### Etap 4 — Decyzje intraday (bramkowane najmocniej, ~1 tydzień+)

Intraday-backtest na akumulowanych barach 5-min z rekordera (dlatego
rekorder musi chodzić od zaraz — Etap 1); model filla uwzględnia **15-min
opóźnienie feedu** + spread + wolumen. Kandydujące reguły: egzekucja stopa w
trakcie sesji (zamiast next-open), opóźnione wejście po gapie. Dopiero
pozytywny walk-forward OOS promuje regułę do „ticku decyzyjnego" w ciągu
dnia. Real-time przez bossaAPI = osobny adapter piszący do tego samego
`prices_intraday` (przyszłość).

## Szkice konfiguracji i schematu

```yaml
# config/schedule.yaml
window: {start: "07:00", end: "19:00", tz: Europe/Warsaw, days: [mon,tue,wed,thu,fri]}
jobs:
  - {name: collect,    every_min: 15}
  - {name: intraday,   every_min: 5, window: ["09:00","17:10"]}
  - {name: llm_events, every_min: 30, budget_pln_day: 2.0}
  - {name: digest,     at: "07:30"}
  - {name: evening,    at: "18:15", chain: [ingest, llm, signals]}
  - {name: health,     at: "19:00"}
```

```sql
CREATE TABLE schedule_runs (job TEXT, scheduled_for TEXT, started_at TEXT,
  finished_at TEXT, status TEXT, detail TEXT);
CREATE TABLE alert_log (user_id TEXT, kind TEXT, dedup_key TEXT UNIQUE,
  priority INTEGER, sent_at TEXT, payload TEXT);
```

## Ryzyka / założenia

- **Feed Yahoo**: opóźniony 15 min, luki (CCC/SPL) — nigdy nie jest
  referencją egzekucji; to świadome ograniczenie do czasu bossaAPI.
- **Host**: Mac usypia — launchd łagodzi, ale wiarygodne okno 7–19 docelowo
  wymaga VPS (monolit się nie zmienia, tylko miejsce uruchomienia).
- **Koszt LLM** przy obecnym wolumenie komunikatów: pojedyncze złote
  miesięcznie (tania ekstrakcja + cache); twardy dzienny limit w configu.
- Historia intraday jest młoda — Etap 4 potrzebuje tygodni nagrywania,
  dlatego jest ostatni, a rekorder startuje pierwszy.

## Czego świadomie NIE robimy

Realnego tradingu, LLM w ścieżce pieniędzy, mikroserwisów, płatnych feedów
na start.
