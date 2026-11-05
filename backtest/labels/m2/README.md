# M2: калибровочный раунд

Схема — **v2** (`../schema.md`). Цель: ≥100 размеченных эпизодов и ≥20
позитивов на горизонт 24/72/168 ч, включая случайные негативы. Brier
публикуется только после прохождения порога; до этого считаются lead-time и
precision/recall.

## Кого берём

- театры `EU_RU`, `ISRAEL`, `GULF`, `MENA`;
- только эпизоды с якорным баллом `score_initial` — он пишется с
  `2026-10-31T23:15Z`, более ранние эпизоды для Brier не годятся;
- только эпизоды старше 168 ч: закрыты все горизонты, включая 7-дневный,
  поэтому метка не требует последующего обновления `observed_until`;
- страты `alert → precursor → random → near_miss`, без дублей; случайные
  негативы обязательны.

## Экспорт

Выполняется на VPS по живой БД. Каждый раунд — свой каталог с датой:

```
cd /opt/tripwire
TRIPWIRE_SECRETS=/nonexistent TRIPWIRE_TRANSLATE=0 \
  .venv/bin/python -m tripwire --export-labels \
  --labels-out /opt/tripwire/backtest/labels/m2/2026-11-04 \
  --labels-since-days 365 --labels-negatives 15 --labels-precursor 30 \
  --labels-near-miss 50 --labels-require-anchor --labels-min-age-hours 168 \
  --labels-exclude /opt/tripwire/backtest/labels/m2
```

`--labels-require-anchor` исключает эпизоды без якоря,
`--labels-min-age-hours 168` — открытые окна. Порог 168, а не 72: при
72 часах `observed_until` негатива не доходит до `first_seen + 168h`, и
7-дневный горизонт остаётся без негативов. Окно в 365 дней не раздувает
выборку: эпизоды старше 31.10.2026 отсекает фильтр якоря.

Ритм — один каталог в неделю: `m2/2026-11-04`, `m2/2026-11-11`, … Повторный
запуск в тот же каталог запрещён (защита от перезаписи уже размеченных
файлов), `--labels-force` в недельном цикле не используется.

`--labels-exclude` — реестр завершённых меток. Каталог сканируется по
`labels.jsonl`; метка считается завершённой, если заполнен `case_type`.
Исключение идёт по `candidate_id` и по `cluster_id`, поэтому один кластер даёт
одну метку, а новый раунд — это дифф, а не повторная выборка. Шаблон
`labels.template.jsonl` и черновики (`labels.agent.jsonl`) реестром не
считаются.

Экспорт пишет `labels.template.jsonl`: по строке на кандидата, `case_type:
null`, `label_id = L<YYYY-MM>-<cluster_id>`. Разметчик получает
`candidates.jsonl`, `labels.template.jsonl` и `manifest.json`; `sealed.jsonl`
не показывается до фиксации меток.

Первый выпуск — после того, как первые якорные эпизоды закроют 72 часа:
`2026-11-03T23:47Z`; практическая дата первого выпуска — `2026-11-04`.

## Разметка

Правила те же, что в `../schema.md`, с поправками v2:

- субъект — уровень эпизода (актор + цель/локация), не «любое событие страны»;
- кинетика: tier 2 допустим, `high` — tier 0/1 или ≥2 независимых домена,
  одиночный tier 2 — `medium`;
- `non_material` — риторика по безопасности/внешней политике;
  `out_of_scope` — быт, культура, внутренняя социальная политика и несовпадение
  субъекта с театром.

Раунд копируется в локальное зеркало `backtest/labels/m2/<дата>/` —
`candidates.jsonl`, `labels.template.jsonl` и `manifest.json`. `sealed.jsonl`
в зеркало не попадает: он остаётся на VPS, чтобы агент-разметчик не мог
открыть баллы. Метки заполняются в `labels.jsonl` из `labels.template.jsonl`.
Пилот v1 в M2 не входит и с этими метками не смешивается.

## Согласие и калибровка

Согласие двух разметчиков считается по `case_type` после того, как оба
закончили раунд:

```
python -m tripwire --kappa-labels --kappa-dir backtest/labels/m2/<дата> \
  --kappa-json backtest/labels/m2/<дата>/agreement.json
```

Команда берёт `labels.jsonl` и `labels.agent.jsonl`, применяет `supersedes`,
пропускает незаполненные `case_type` и пишет `n`, `po`, `pe`, `kappa`, 95% CI
(bootstrap 1000, seed 42), распределения и расхождения с заметками. Меньше
5 пар — печатается «недостаточно меток», kappa не считается. `sealed.jsonl`
не читается.

## Покрытие 168 ч для раунда 2026-11-04

Первый раунд выпущен с минимумом 72 ч, поэтому его негативы не покрывают
7-дневный горизонт. Через неделю после якоря строится шаблон заменяющих
меток:

```
python -m tripwire --coverage-labels \
  --coverage-round backtest/labels/m2/2026-11-04
```

В `backtest/labels/m2/coverage/2026-11-04/` пишутся `labels.template.jsonl`
(только негативы с недостающим покрытием, `supersedes` заполнен,
`target_until` — минимально допустимый `observed_until`) и `manifest.json`.
Разметчик перепроверяет эпизод и пишет `labels.jsonl` в тот же каталог:
`case_type` остаётся прежним, если материального развития не было, иначе
`anticipatory` с `outcome_at`; `observed_until` — фактическое время проверки,
не раньше `target_until`. `labels.jsonl` каталога `coverage/` подхватывается
`--merge-labels` наравне с раундами. Сам шаблон меткой не считается.

Для будущих раундов отдельный проход не нужен: экспорт идёт с `168`.

## Калибровка

Перед расчётом недельные раунды склеиваются в один каталог:

```
python -m tripwire --merge-labels --labels-merge-root backtest/labels/m2 \
  --labels-merge-out backtest/labels/m2/aggregate
python -m tripwire --calibrate-labels \
  --calibration-dir backtest/labels/m2/aggregate \
  --calibration-json backtest/labels/m2/aggregate/calibration.json
```

`--merge-labels` применяет `supersedes` (остаётся текущая версия метки),
дедуплицирует `sealed.jsonl`, предпочитая строку с непустым `score_initial`,
пропускает неполные метки и печатает конфликты. `aggregate/` исключён из
повторного сканирования, поэтому слияние можно запускать после каждого раунда.

Отчёт даёт Brier, базу, skill и 95% CI по кластерам и дням для каждого
горизонта. При N < 100 или < 20 позитивов печатается «недостаточно данных».
