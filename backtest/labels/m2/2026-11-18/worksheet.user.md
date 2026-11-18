# Рабочий лист человеческой разметки — раунд M2 2026-11-18

Подмножество: 25 из 93 кандидатов, отобрано случайно (sha1 от `"m2-human-2026-11-18:<cluster_id>"`),
без использования баллов, уровней и страт. Порядок — по времени якоря.

Полные правила: `../LABELING.md`. Схема: `../../schema.md` (v2.1).

## Как заполнять

В каждом блоке есть строка `ОТВЕТ>`. Впишите после неё три поля через `;`.
Пример заполненной строки (сдвинут, чтобы не считался ответом):

```
  ОТВЕТ> case_type=non_material; почему=только заявление, действий нет; ссылка=https://...
```

- `case_type` — ровно одно из: `out_of_scope`, `non_material`, `detection`, `anticipatory`, `unresolved`.
- `почему` — одна строка по-русски: что именно вы увидели и почему это такой тип.
- `ссылка` — публичный URL статьи, которую вы реально открыли. Если проверяли только якорь — оставьте пустым.
- Не нашли ничего надёжного — ставьте `unresolved`, это честный ответ, а не пропуск.
- Остальные поля JSONL (outcome, severity, observed_until, tier, label_id) заполню механически по схеме.

## Что смотреть

Субъект — актор + цель/локация этого эпизода, не «любое событие страны».
Окно — 168 ч после якоря. События позже конца окна не считаются.

- `out_of_scope` — быт, культура, внутренняя социальная политика, церемонии; или субъект не из театра.
- `non_material` — риторика/политика/бюджет по безопасности без материального действия.
- `detection` — материальное событие той же ситуации уже было на якоре, нового после него нет.
- `anticipatory` — новое материальное развитие той же ситуации после якоря, в окне.
- `unresolved` — проверить нельзя.

Частые ловушки v2.1: очередной рутинный повтор серии — не исход; санкции и designation — `non_material` severity 1;
подтверждение/опровержение/атрибуция после якоря — не исход; снятие ограничений и штатная работа — не исход;
реализация чужой угрозы — исход исполнителя, а не предупредившего.

---

## 01 · cluster 10184 · EU_RU · —

**Почти полмиллиона человек вышли на протесты в Европе в минувшие выходные**

Якорь: `2026-11-02T09:30:33Z` · окно до `2026-11-09T09:30:33Z`

Источники:
- [tier 2] ria.ru · 2026-11-02T09:20:12Z
  Почти полмиллиона человек вышли на протесты в Европе в минувшие выходные
  https://ria.ru/20261102/evropa-2128598044.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 02 · cluster 10191 · EU_RU · —

**В Харьковской области прогремел взрыв**

Якорь: `2026-11-02T09:30:33Z` · окно до `2026-11-09T09:30:33Z`

Источники:
- [tier 2] ria.ru · 2026-11-02T09:10:59Z
  В Харьковской области прогремел взрыв
  https://ria.ru/20261102/kharkovskaya-oblast-2128593478.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 03 · cluster 10928 · EU_RU · —

**В Ровно прогремел взрыв**

Якорь: `2026-11-03T00:56:46Z` · окно до `2026-11-10T00:56:46Z`

Источники:
- [tier 2] ria.ru · 2026-11-03T00:55:07Z
  В Ровно прогремел взрыв
  https://ria.ru/20261103/vzryv-2128801373.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 04 · cluster 12424 · ISRAEL · IL

**Tucker Carlson: Donald Trump pressured by Benjamin Netanyahu to launch nuclear strike on Iran**

Якорь: `2026-11-04T07:20:17Z` · окно до `2026-11-11T07:20:17Z`

Источники:
- [tier 2] jpost.com · 2026-11-03T04:34:23Z
  Tucker Carlson: Donald Trump pressured by Benjamin Netanyahu to launch nuclear strike on Iran
  https://news.google.com/rss/articles/CBMiakFVX3lxTE9fMldaSkQ4TVYzWXdWLXRoaGc5cHRlZnVzanNrZWlOWFNJZExIMnRNcDFObnJROU5rZGNuQ2xVWjFzZzFIWEtoeGp5RHl4WHh6UzBPcGpKaXYxeDIwUDdwcTNZOEtPdDNLbHc?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 05 · cluster 13090 · EU_RU · —

**В Сумах прогремел взрыв**

Якорь: `2026-11-04T19:39:32Z` · окно до `2026-11-11T19:39:32Z`

Источники:
- [tier 2] ria.ru · 2026-11-04T19:11:48Z
  В Сумах прогремел взрыв
  https://ria.ru/20261104/vzryv-2129248064.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 06 · cluster 13562 · EU_RU · —

**EU Statement – UN General Assembly 3rd Committee: Interactive Dialogue on the use of mercenaries**

Якорь: `2026-11-05T08:18:44Z` · окно до `2026-11-12T08:18:44Z`

Источники:
- [tier 1] eeas.europa.eu · 2026-11-04T23:08:33Z
  EU Statement – UN General Assembly 3rd Committee: Interactive Dialogue on the use of mercenaries
  https://news.google.com/rss/articles/CBMi0wFBVV95cUxQSDByTy1xZDNFU3AyRXhkTGhmZTY0eHhhZEFieTA4WmNOb3QxTlFoWW9qalZGSm91R3Q2S0VlYXY5TEl3ZDAxV3pId3ZnX0tkZzBzdkVKMld4eG9weW05alJMczFDLWFCazhOdTVBck1Idzh5UzNKT0FZcklHRG5TbVN4bnJreTluS2RXTUdreGd0dzJfLTBySEl5M2JvaE95NS15VE9CYlFtMWxsY3BFV0ttUVFaN1ZHQTg3VGk2RTFmNzRrWjBiQlFvNVF0cmE3cDM4?oc=5
  > EU Statement – UN General Assembly 3rd Committee: Interactive Dialogue on the use of mercenaries European External Action Service (EEAS)

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 07 · cluster 13754 · MENA · TR

**Турция летом 2027 года может потерять часть российского турпотока из-за роста цен**

Якорь: `2026-11-05T10:36:51Z` · окно до `2026-11-12T10:36:51Z`

Источники:
- [tier 2] interfax.ru · 2026-11-05T10:29:00Z
  Турция летом 2027 года может потерять часть российского турпотока из-за роста цен
  https://interfax.ru/world/1127159
  > Турция может недосчитаться около 1,5 млн российских туристов в 2027 году из-за подорожания отдыха и конкуренции с другими направлениями, считает гендиректор сети турагентств "Розовый слон" Алексан Мкртчян."В Турции растут цены на продукты, электричество, канализацию: в общем, расходы повышаются на все, включая зарплаты персонала.

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 08 · cluster 13866 · EU_RU · —

**Во Франции продолжаются массовые акции протеста**

Якорь: `2026-11-05T12:05:33Z` · окно до `2026-11-12T12:05:33Z`

Источники:
- [tier 2] interfax.ru · 2026-11-05T11:56:00Z
  Во Франции продолжаются массовые акции протеста
  https://interfax.ru/world/1127184
  > Жители Франции вновь выходят на масштабные протесты, сообщает в четверг газета Le Figaro.

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 09 · cluster 14606 · ISRAEL · —

**Stewardship for Post-remediation Management of Radioactively Contaminated Land**

Якорь: `2026-11-06T00:29:27Z` · окно до `2026-11-13T00:29:27Z`

Источники:
- [tier 0] iaea.org · 2026-11-05T15:50:29Z
  Stewardship for Post-remediation Management of Radioactively Contaminated Land
  https://news.google.com/rss/articles/CBMiS0FVX3lxTE1zaTMxQmthNUZURDc3ei1zNUZqYjVJdFI2YXZrS3ktMmRRcW5YcEZWLXhDeFlId2paZ2FnZ0UwVFRVWTJQVEx0Ti1EMA?oc=5
  > Stewardship for Post-remediation Management of Radioactively Contaminated Land International Atomic Energy Agency

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 10 · cluster 14830 · EU_RU · US

**Рютте дал совет Трампу после выборов в США**

Якорь: `2026-11-06T06:42:41Z` · окно до `2026-11-13T06:42:41Z`

Источники:
- [tier 2] ria.ru · 2026-11-06T06:32:40Z
  Рютте дал совет Трампу после выборов в США
  https://ria.ru/20261106/ryutte-2129606735.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 11 · cluster 15612 · GULF · AE

**Путин пригласил переговорщиков, которые поедут в ОАЭ, на совещание с СБ**

Якорь: `2026-11-06T17:57:43Z` · окно до `2026-11-13T17:57:43Z`

Источники:
- [tier 2] ria.ru · 2026-11-06T17:32:32Z
  Путин пригласил переговорщиков, которые поедут в ОАЭ, на совещание с СБ
  https://ria.ru/20261106/delegatsiya-2129839659.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 12 · cluster 15690 · EU_RU · —

**Acting Navy Secretary Visits Blount Island, Observes How It Equips Marines Worldwide**

Якорь: `2026-11-06T19:28:17Z` · окно до `2026-11-13T19:28:17Z`

Источники:
- [tier 0] war.gov · 2026-11-06T19:02:53Z
  Acting Navy Secretary Visits Blount Island, Observes How It Equips Marines Worldwide
  https://war.gov/News/News-Stories/Article/Article/4643614/acting-navy-secretary-visits-blount-island-observes-how-it-equips-marines-world
  > Acting Navy Secretary Hung Cao visited Blount Island, Fla., to see how its workforce and waterfront facilities prepare and position equipment for Marines worldwide. The visit highlighted the installation's role in sustaining deployed forces and supporting fleet readiness.

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 13 · cluster 15928 · EU_RU · FI

**Visa requirement and travel documents accepted by Finland**

Якорь: `2026-11-07T02:09:09Z` · окно до `2026-11-14T02:09:09Z`

Источники:
- [tier 1] um.fi · 2026-11-05T06:09:54Z
  Visa requirement and travel documents accepted by Finland
  https://news.google.com/rss/articles/CBMi6AFBVV95cUxOd0hick5VckNJckFYZmg0V1MzVlpEUTNFX2RLb2E2UGozc0d1MktLZi0xb1dvYjl5VjM5cUYydUtEazhNYndsMXVXVFNudmwzc0Rjc2NXYjdfeUk2VjJzaXhSTGxBc200UllHLUVMSGlLODl1ZWpBSG8wbk9Ycy1ncGV0Z2RUZmhWNnRjT1F5a2w4QkNJUkFZd1NKTjFJX2hkQ050SlpLbURnaWhYVjd0bmlnNnhTMDJLajVRMWdoVWxFOHgtbmduRzZONV91MEp1aERjN1daX3JlbDJ3VTRCOFJnbDB0dzA5?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 14 · cluster 16058 · EU_RU · —

**"Творит чудеса". Тренер назвал Капризова одним из самых элитных форвардов**

Якорь: `2026-11-07T06:47:47Z` · окно до `2026-11-14T06:47:47Z`

Источники:
- [tier 2] ria.ru · 2026-11-07T06:39:02Z
  "Творит чудеса". Тренер назвал Капризова одним из самых элитных форвардов
  https://ria.ru/20261107/khokkey-2129898151.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 15 · cluster 16504 · MENA · TR

**Турция продолжит вносить вклад в урегулирование на Украине, заявил Эрдоган**

Якорь: `2026-11-07T17:49:23Z` · окно до `2026-11-14T17:49:23Z`

Источники:
- [tier 2] ria.ru · 2026-11-07T17:37:11Z
  Турция продолжит вносить вклад в урегулирование на Украине, заявил Эрдоган
  https://ria.ru/20261107/erdogan-2130018221.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 16 · cluster 16749 · EU_RU · —

**В Луцке прогремел взрыв**

Якорь: `2026-11-08T01:17:08Z` · окно до `2026-11-15T01:17:08Z`

Источники:
- [tier 2] ria.ru · 2026-11-08T01:01:46Z
  В Луцке прогремел взрыв
  https://ria.ru/20261108/ukraina-2130049357.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 17 · cluster 16775 · ISRAEL · —

**IDF needs 12,000 more soldiers as political compromises threaten Haredi recruitment efforts**

Якорь: `2026-11-08T02:29:11Z` · окно до `2026-11-15T02:29:11Z`

Источники:
- [tier 2] ynetnews.com · 2026-11-08T02:09:01Z
  IDF needs 12,000 more soldiers as political compromises threaten Haredi recruitment efforts
  https://news.google.com/rss/articles/CBMibkFVX3lxTE5xWF8wbmozN21pWGdNaEpObDZUQS01d0dEV213c1RUdlA4VlU2UGJtN3hRTEgxYVBPZTNBc1NsOUtnZExGQ1Z4cjk2aXUzV3RmSEVKOHRldzEwMFZZMi1PLW5aR2FzT290cmIyaWNR?oc=5
- [tier 2] ynetnews.com · 2026-11-08T03:42:58Z
  Don't use IDF reservists as political bargaining chips: The army needs soldiers, not political deals
  https://news.google.com/rss/articles/CBMibkFVX3lxTFBRTnJmdFUwN2w0NzFwcV9yTk42ei1mTEg3TmNjbjdEM283LWlzV1NZNjVnN3FWeVg5YTZxN25CdUNWcXVoOS03OE4wRlQ2blFFdnlqdXd2b1l0b28yaU9uX3NnNUxLYXk1aG9GVm9R?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 18 · cluster 16797 · ISRAEL · IL

**Abdul El-Sayed: The face of extremism through democratic channels**

Якорь: `2026-11-08T03:11:22Z` · окно до `2026-11-15T03:11:22Z`

Источники:
- [tier 2] blogs.timesofisrael.com · 2026-11-07T15:02:00Z
  Abdul El-Sayed: The face of extremism through democratic channels
  https://news.google.com/rss/articles/CBMingFBVV95cUxOQ3p4VU5YR1hJbDUyTXdTaTlpMFJwMnBsdHotZU9STUlrbmxMTTlDUUFYMmlUUEJhYXFkNU8yNFRhTU9UNkJ4UTRGSTdYcVdPaVdWZVh4aGVEMjRCSkxKTllhTzR2YjJPQThwbmVoTTd6dVN3TW1jel9telZJWjRLaTF2VDN5QmZ4OG9sYlVLOGYwX1NvdDRxVzNiYktkZw?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 19 · cluster 17169 · EU_RU · —

**В Харькове прогремел взрыв**

Якорь: `2026-11-08T13:42:50Z` · окно до `2026-11-15T13:42:50Z`

Источники:
- [tier 2] ria.ru · 2026-11-08T13:34:25Z
  В Харькове прогремел взрыв
  https://ria.ru/20261108/harkov-2130130922.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 20 · cluster 17301 · MENA · TR

**Турция запустит ИИ-систему для контроля телеэфира и цифровых платформ**

Якорь: `2026-11-08T17:17:53Z` · окно до `2026-11-15T17:17:53Z`

Источники:
- [tier 2] ria.ru · 2026-11-08T16:52:49Z
  Турция запустит ИИ-систему для контроля телеэфира и цифровых платформ
  https://ria.ru/20261108/turtsija-2130162368.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 21 · cluster 17752 · ISRAEL · —

**Tanto Minchiata (@tantominchiata)**

Якорь: `2026-11-09T06:13:18Z` · окно до `2026-11-16T06:13:18Z`

Источники:
- [tier 2] substack.com · 2026-11-08T20:45:19Z
  Tanto Minchiata (@tantominchiata)
  https://news.google.com/rss/articles/CBMiY0FVX3lxTE02U2ZjbUpLa0hRMGZsUXZIVU81aDV4ZHljcmZKZnlWa3k3ZlFvZXpvTlNka1llVU9DMUJzeVZyUUFhOU80bUo1NC1zN1NaWkRpakpWWVpObXFZR1g5bFVCbkdtYw?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 22 · cluster 18326 · GULF · —

**Rain Falls on Rafha**

Якорь: `2026-11-09T14:18:37Z` · окно до `2026-11-16T14:18:37Z`

Источники:
- [tier 1] spa.gov.sa · 2026-11-07T15:00:38Z
  Rain Falls on Rafha
  https://news.google.com/rss/articles/CBMiREFVX3lxTFBNU3pvVHlwNzJ2MC1KWTc3Vlg0Q0t4eEdRN1FBV3pNczRSaXlDbXFTQndRTC1IYzZkdWY1MGdCc2FqSE1i?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 23 · cluster 18482 · GULF · SA

**Transport Ministry, University of Jeddah Launch 'The Container' Incubator to Develop Innovative Transport Solutions**

Якорь: `2026-11-09T16:46:16Z` · окно до `2026-11-16T16:46:16Z`

Источники:
- [tier 1] spa.gov.sa · 2026-11-09T12:36:33Z
  Transport Ministry, University of Jeddah Launch 'The Container' Incubator to Develop Innovative Transport Solutions
  https://news.google.com/rss/articles/CBMiSkFVX3lxTE5EaUZCTUJpblJxUHNYNElfUVlVd3R3clYzRDNFN292Y2V4WEE1ZEQ1RW5jN3Q4TzFILUhCSmFwNjdMNnAwSWhsTUR3?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 24 · cluster 18891 · EU_RU · —

**В Кировограде прогремел взрыв**

Якорь: `2026-11-10T04:38:55Z` · окно до `2026-11-17T04:38:55Z`

Источники:
- [tier 2] ria.ru · 2026-11-10T04:21:27Z
  В Кировограде прогремел взрыв
  https://ria.ru/20261110/vzryv-2130456002.html

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---

## 25 · cluster 20106 · GULF · —

**Islamic University Discusses Shariah Rulings and Contemporary AI Issues**

Якорь: `2026-11-11T05:36:35Z` · окно до `2026-11-18T05:36:35Z`

Источники:
- [tier 1] spa.gov.sa · 2026-11-11T03:12:00Z
  Islamic University Discusses Shariah Rulings and Contemporary AI Issues
  https://news.google.com/rss/articles/CBMiSkFVX3lxTE5DbTFuU2kycVBtNk9VVUZGVlRBS09VcW9ud1dYYS1USmxubWpzQzNyN3JtMzBGcmMtVWlOTjhqNWJCLVBpSmFrbjlB?oc=5

```
ОТВЕТ> case_type=; почему=; ссылка=
```

---
