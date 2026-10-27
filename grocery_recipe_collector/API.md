# API каталога рецептов и товаров

Базовый адрес при запуске Compose: `http://127.0.0.1:18082`. API принимает только чтение и не меняет приложение на порту 18081. В контейнер смонтирован один файл каталога в режиме чтения.

| Запрос | Ответ |
| --- | --- |
| `GET /health` | Версия, дата, счётчики, ограничения полноты |
| `GET /v1/recipes` | Все рецепты с пагинацией |
| `GET /v1/recipes/1` | Рецепт, ингредиенты с количествами, ID подходящих товаров |
| `GET /v1/ingredients` | 29 ингредиентов с исходными ID и рецептами |
| `GET /v1/ingredients/8/products?store=magnit` | Все собранные оливковые масла Магнита |
| `GET /v1/products?store=pyaterochka&ingredient_id=2&complete=1` | Молоко с составом, ценой и пригодными КБЖУ |
| `GET /v1/products/perekrestok:82036` | Конкретный товар, если такой ID присутствует в текущем сборе |
| `GET /v1/coverage` | Покрытие каждого ингредиента по каждой сети |

Для списков поддерживаются `limit` (1–250, по умолчанию 50), `offset`, `q`. Ответ: `count`, `limit`, `offset`, `next_offset`, `results`. У `/v1/coverage` доступны `store` и `ingredient_id`. Неизвестный ID возвращает 404; некорректный фильтр — 400.

Фильтр `store`: `perekrestok`, `pyaterochka`, `magnit`. `include_candidates=1` добавляет карточки без состава; по умолчанию возвращаются только записи `offers`. `complete=1` требует непустой состав, цену и пригодные КБЖУ. Этот фильтр не подтверждает свежесть цены или наличие по адресу.

## Просмотр в браузере

Откройте http://127.0.0.1:18082/browse, выберите рецепт или ингредиент и при необходимости отфильтруйте магазин. Страница использует тот же экспорт, что и API; SQLite-файл содержит те же собранные товары. Исходное приложение на порту 18081 и его база данных не изменяются.

## Товар

`product_id` имеет вид `сеть:артикул`. Артикулы разных сетей не считаются одним товаром. Запись содержит:

- `product_name`, `brand`, `manufacturer`, `external_id`, `source_url`;
- `ingredients_text`, `composition_complete`, `allergen_warning`;
- `nutrition`: `energy_kcal`, `protein_g`, `fat_g`, `carbohydrate_g`, `basis_quantity`, `basis_unit`;
- `nutrition_complete` — все четыре числа и база опубликованы; `nutrition_usable` дополнительно исключает явно противоречивые значения и нулевые шаблоны;
- `price_rub`, `regular_price_rub`, `price_unit`, `currency`, `unit_pricing`;
- `package.quantity`, `package.unit`, `package.drained_quantity_g`;
- `price_observations` — отдельные найденные цены и контексты адресов одного артикула; для объединения не выбирается минимальная цена из разных городов;
- `availability`, `price_freshness`, `price_observed_at`, `indexed_at_label`, `source_retrieved_at`, `retailer_context`;
- `ingredient_ids`, `recipe_ids`, `match_status`, `quality_flags` и ссылки на источники в `evidence`.

`null` означает отсутствие сведений. Ноль используется только если он опубликован источником. GTIN и изображения в этой выгрузке не собирались и оставлены `null`.

### Цены и единицы

`price_unit: kg` означает цену килограмма, `package` — цену показанной упаковки/количества. Показанная сумма весового товара и цена килограмма различаются. `unit_pricing` рассчитывает цену кг, литра или штуки только при известной совместимой фасовке; плотность масла/молока не предполагается.

Упаковка консервов содержит жидкость. Для рецептов с нутом/фасолью «без жидкости» нельзя автоматически считать массу всей банки съедобной порцией. Если масса без жидкости отсутствует, сохраняется флаг `drained_weight_not_published`.

Количества овощей в рецептах относятся к съедобной части. Потери на очистку не заданы. Стоимость рецепта, число упаковок и КБЖУ при выборе товара автоматически не пересчитываются. Рецептурные значения исходного приложения сохранены как `nutrition_from_app` с `nutrition_coverage_from_app`.

### Проверка качества

`match_status: name_matched` — соответствие названию базового ингредиента по консервативным правилам, а не оценка «чистого состава». Сладкие йогурты, готовые блюда, сухие бобовые вместо готовых и посторонние результаты выдачи исключены. Результаты всё равно следует оценивать по реальному составу и ограничениям конкретного рецепта.

Флаги `zero_nutrition_from_source`, `inconsistent_nutrition_from_source`, `conflicting_availability`, `missing_price`, `incomplete_nutrition` сохраняют ошибки/противоречия источника; числа не исправляются по средним значениям.

Полнота ассортимента не подтверждена: `all_possible_products_collected: false`. 28 покупаемых ингредиентов проверены в трёх сетях. Везде указывается состав именно карточки товара, без подстановки состава другого SKU. Вода не требует покупки.

## Повторный сбор

```sh
python3 catalog.py export-app --base-url http://127.0.0.1:18081 --out data/new_run
python3 catalog.py build --data data/new_run
```

Между этими командами нужно собрать официальные карточки в `data/new_run/evidence/search_*.json`. Контракт записи: `ingredient_id`, `ingredient_name`, `store`, `query`, `pass`, `collected_at` (UTC ISO 8601), `result` (текст официальных карточек из веб-инструмента). Сборщик `collect.py` отдельно умеет открывать публичные карточки через Camoufox; блокировки фиксируются, поля не выдумываются.

`catalog.py build` создаёт `catalog.json`, `catalog.json.gz`, `products.jsonl`, `catalog.sqlite3`, `coverage.json`, `summary.json`, список отклонений и журнал поиска. Сырые ответы приложения и карточек сохраняются локально в `evidence/` и не публикуются в Git. Для текущей выгрузки официальный веб-поиск выполнен в этом сеансе; автоматического автономного поиска через поисковый сервис в Python-коде нет.

SQLite содержит таблицы `recipes`, `ingredients`, `recipe_ingredients`, `products`, `ingredient_products`; `data_json` сохраняет полную запись без потери дополнительных полей.

```sql
SELECT p.name, p.ingredients_text, p.price_rub, p.source_url
FROM products p
JOIN ingredient_products ip ON ip.product_id = p.id
WHERE ip.ingredient_id = 8 AND p.store = 'magnit' AND p.eligible_for_offers = 1;
```
