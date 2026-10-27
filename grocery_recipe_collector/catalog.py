#!/usr/bin/env python3
"""Export recipes from a Ladno API and build an evidence-backed grocery dataset.

No product facts are copied from another size, retailer or recipe reference table.
Web search evidence is supplied as JSON records (see API.md). The separate
collect.py entry point performs direct browser collection through Camoufox.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import gzip
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import parse_qs, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

STORES = {"perekrestok": "Перекрёсток", "pyaterochka": "Пятёрочка", "magnit": "Магнит"}
HOSTS = {"perekrestok.ru": "perekrestok", "www.perekrestok.ru": "perekrestok", "5ka.ru": "pyaterochka", "magnit.ru": "magnit"}
NUM = r"\d+(?:[.,]\d+)?"
MONEY = r"[\d\s\u00a0\u2009\u202f]+(?:[.,]\d{1,2})?"


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def normalize(text):
    return re.sub(r"\s+", " ", text).strip()


def number(text):
    if text is None:
        return None
    text = re.sub(r"\s", "", str(text)).replace(",", ".")
    try:
        value = Decimal(text)
        return float(value) if value.is_finite() and value >= 0 else None
    except InvalidOperation:
        return None


def canonical_url(url):
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc.lower(), parsed.path.rstrip("/"), "", "", ""))


def retailer(url):
    parsed = urlparse(url)
    store = HOSTS.get(parsed.netloc.lower())
    if not store:
        return None
    if store == "perekrestok" and not re.search(r"/cat/\d+/p/[^/]+$", parsed.path.rstrip("/")):
        return None
    if store in ("magnit", "pyaterochka") and not re.search(r"^/product/[^/]+/?$", parsed.path):
        return None
    return store


def external_id(url):
    p = urlparse(url).path.rstrip("/")
    if '/product/' in p and urlparse(url).netloc == 'magnit.ru':
        m = re.search(r"/product/(\d+)", p)
    else:
        m = re.search(r"-(\d+)$", p)
    return m.group(1) if m else None


def public_json(url):
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "grocery-api-recipe-export/2.0"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def export_app(base_url, out):
    out = Path(out)
    origin = urlparse(base_url)
    url = base_url.rstrip("/") + "/api/v1/recipes/"
    recipes, visited = [], set()
    observed_at = timestamp()
    while url:
        if urlparse(url).netloc != origin.netloc or url in visited:
            raise ValueError("Unexpected or cyclic API pagination")
        visited.add(url)
        data = public_json(url)
        if not isinstance(data.get("results"), list):
            raise ValueError("Expected paginated recipes API")
        write_json(out / "evidence" / f"app_recipes_{len(visited):03}.json", data)
        recipes.extend(data["results"])
        url = urljoin(url, data["next"]) if data.get("next") else None
        time.sleep(.2)
    if len({r["id"] for r in recipes}) != len(recipes) or len(recipes) != data["count"]:
        raise ValueError("Recipe pagination count changed during export; retry snapshot")
    write_json(out / "recipes.json", {"source_url": base_url.rstrip('/') + '/api/v1/recipes/', "retrieved_at": observed_at, "count": len(recipes), "recipes": recipes})
    ingredients, products = {}, {}
    for recipe in recipes:
        for ing in recipe["ingredients"]:
            key = ing["ingredient_id"]
            item = ingredients.setdefault(key, {"ingredient_id": key, "name": ing["name"], "purchase_required": ing.get("purchase_required", True), "units": [], "recipe_ids": [], "requirements": [], "app_product_ids": []})
            if ing["unit"] not in item["units"]:
                item["units"].append(ing["unit"])
            item["recipe_ids"].append(recipe["id"])
            item["requirements"].append({"recipe_id": recipe["id"], "quantity": ing["quantity"], "unit": ing["unit"], "servings": recipe["servings"]})
            for product in ing.get("products", []):
                products[product["id"]] = product
                if product["id"] not in item["app_product_ids"]:
                    item["app_product_ids"].append(product["id"])
    write_json(out / "ingredients.json", {"count": len(ingredients), "ingredients": list(ingredients.values())})
    write_json(out / "app_products.json", {"source": "application_snapshot", "retrieved_at": observed_at, "count": len(products), "products": list(products.values())})
    return {"recipes": len(recipes), "ingredients": len(ingredients), "app_products": len(products)}


def clean_lines(text):
    lines = []
    for line in text.splitlines():
        line = re.sub(r"^L\d+:\s?", '', line)
        line = re.sub(r"【\d+†([^†】]*)(?:†[^】]*)?】", r"\1", line)
        lines.append(re.sub(r"^[#*]+\s*", "", line).strip())
    return lines


def product_name(text, title):
    match = re.search(r"^(?:L\d+:\s*)?#\s+(.+)$", text, re.M)
    if match:
        return normalize(match.group(1))
    return re.split(r"\s+(?:[–—-]\s+купить|:\s*купить)", title, flags=re.I)[0].strip()


STOP_COMPOSITION = re.compile(r"^(?:Состав|Ккал|Калории|Белки|Жиры|Углеводы|Пищевая ценность|Описание|Характеристики|Условия хранения|Рекомендации|Отзывы|Наличие|Информация|Вес(?:,|$)|Бренд(?:\s|$)|Срок|Страна|Производитель|Показать|Читать|Подробнее|В корзину|Добавить|\d+(?:[.,]\d+)?\s*₽)", re.I)


def extract_composition(text):
    lines = clean_lines(text)
    candidates = []
    for index, line in enumerate(lines):
        m = re.match(r"^(?:Состав(?: продукта)?|Ингредиенты)\s*(?::\s*(.*))?$", line, re.I)
        if not m:
            continue
        inline = (m.group(1) or '').strip()
        parts = [inline] if inline else []
        for next_line in lines[index + 1:]:
            if not next_line:
                continue
            if STOP_COMPOSITION.search(next_line) or next_line.startswith('---'):
                break
            if re.fullmatch(r"(?:[\d.,]+\s*г?|\*\s*\*)", next_line):
                break
            # Prices and review headers after an inline composition are not ingredients.
            if re.search(r"₽|оценк|отзыв|Все$", next_line, re.I):
                break
            parts.append(next_line)
            if sum(map(len, parts)) > 5000:
                break
        result = normalize(" ".join(parts)).strip(" :;")
        if len(result) >= 2 and not re.fullmatch(r"[\d.,%\s]+|Состав|Не указан|Нет данных", result, re.I):
            complete = not bool(re.search(r"\.\.\.|…|\[\.\.\.\]|подробнее", result, re.I))
            candidates.append((result, complete))
    return max(candidates, key=lambda x: (x[1], len(x[0]))) if candidates else (None, False)


def extract_nutrition(text):
    lines = [line for line in clean_lines(text) if line]
    start = next((i for i, line in enumerate(lines) if re.search(r"Пищевая ценность.*100|Пищевая ценность в 100", line, re.I)), None)
    result = {"basis_quantity": 100, "basis_unit": None, "energy_kcal": None, "protein_g": None, "fat_g": None, "carbohydrate_g": None}
    if start is None:
        return result
    heading = lines[start]
    result['basis_unit'] = 'ml' if re.search(r"100\s*мл", heading) else 'g' if re.search(r"100\s*(?:г|грамм)", heading) else None
    section = lines[start + 1: start + 20]
    value_before_label = bool(section and re.fullmatch(NUM, section[0]))
    names = {"energy_kcal": r"^(?:Калории|Ккал)$", "protein_g": r"^белки$", "fat_g": r"^жиры$", "carbohydrate_g": r"^углеводы$"}
    for field, pat in names.items():
        for i, line in enumerate(section):
            if re.match(pat, line, re.I):
                # 5ka prints the number before its label; Perekrestok/Magnit after it.
                before = section[i - 1] if i else ''
                after = section[i + 1] if i + 1 < len(section) else ''
                if value_before_label and i and re.fullmatch(NUM, before):
                    result[field] = number(before)
                elif not value_before_label and re.fullmatch(NUM + r"\s*г?", after):
                    result[field] = number(re.sub(r"\s*г$", '', after))
                break
    return result


def value_after(text, label):
    lines = [line for line in clean_lines(text) if line]
    for i, line in enumerate(lines):
        m = re.fullmatch(label + r"\s+(.+)", line, re.I)
        if m:
            return m.group(1)
        if re.fullmatch(label, line, re.I) and i + 1 < len(lines):
            return lines[i + 1]
    return None


def extract_package(name, text):
    pattern = rf"({NUM})\s*(кг|мл|литр(?:а|ов)?|л|г|шт(?:ук)?)(?=\b|[.,])"
    matches = list(re.finditer(pattern, name, re.I))
    if not matches:
        return {"quantity": None, "unit": None, "source": None, "drained_quantity_g": None}
    match = matches[-1]
    unit = {'кг': 'kg', 'г': 'g', 'мл': 'ml', 'л': 'l', 'шт': 'pcs', 'штук': 'pcs'}.get(match.group(2).lower(), 'l')
    qty = number(match.group(1))
    # 6x40g must remain 240g, when the card only names individual sachets.
    before = name[:match.start()]
    multi = re.search(r"(\d+)\s*[xх×]\s*$", before)
    if multi:
        qty *= int(multi.group(1))
    drained = re.search(r"(?:масса (?:основного продукта|нетто без жидкости)|без жидкости|масса без заливки)[^\d\n]{0,30}(" + NUM + r")\s*г", text, re.I)
    return {"quantity": qty, "unit": unit, "source": "product_title", "drained_quantity_g": number(drained.group(1)) if drained else None}


def extract_price(text, store):
    text = re.sub(r'^L\d+:\s?', '', text, flags=re.M)
    h1 = re.search(r"^#\s+.+$", text, re.M)
    section = text[h1.end():] if h1 else text
    # Never take a similar/recommended product price from before the product title.
    section = re.split(r"(?:Пищевая ценность|## Состав|Характеристики|Рекомендации|Отзывы)", section, maxsplit=1)[0]
    m = re.search(r"(?:^|\n)(?:Цена\s+)?(" + MONEY + r")\s*₽", section)
    if not m:
        return None, None, None
    raw = m.group(1).strip()
    # Pyaterochka renders 102 99 ₽ with kopeks in a separate span.
    if store == 'pyaterochka' and not re.search('[.,]', raw):
        digits = re.sub(r"\s", '', raw)
        value = float(Decimal(digits) / 100) if len(digits) >= 3 else number(digits)
    else:
        value = number(raw)
    per_kg = re.search(r"(" + MONEY + r")\s*₽\s*/\s*(?:1\s*)?кг", section, re.I)
    unit = 'kg' if re.search(r"за\s*1\s*кг", section, re.I) or per_kg and number(per_kg.group(1)) == value else 'package'
    regular = re.search(r"Старая цена\s+(" + MONEY + r")\s*₽", section)
    return value, unit, number(regular.group(1)) if regular else None


def unit_pricing(price, price_unit, package):
    """Comparable unit prices; mass/volume conversions require no assumed density."""
    result = {'rub_per_kg': None, 'rub_per_l': None, 'rub_per_piece': None}
    if price is None:
        return result
    if price_unit == 'kg':
        result['rub_per_kg'] = price
    elif package['quantity'] and package['unit']:
        qty, unit = package['quantity'], package['unit']
        if unit in ('g', 'kg'):
            result['rub_per_kg'] = round(price / qty * (1000 if unit == 'g' else 1), 4)
        elif unit in ('ml', 'l'):
            result['rub_per_l'] = round(price / qty * (1000 if unit == 'ml' else 1), 4)
        elif unit == 'pcs':
            result['rub_per_piece'] = round(price / qty, 4)
    return result


def matches_ingredient(name, ingredient_id):
    n = name.lower().replace('ё', 'е')
    includes = {
        1: r"(?:хлопья.*овсян|овсян.*хлопья|геркулес)", 2: r"молоко", 4: r"яблок", 5: r"(?:греч|ядрица)",
        6: r"шампиньон", 7: r"лук", 8: r"(?:масло.*олив|олив.*масло)", 9: r"чечевица.*красн|красн.*чечевица",
        10: r"морковь", 11: r"помидор|томат", 12: r"(?:филе|грудк).*(?:кур|цыплен)|(?:кур|цыплен).*(?:филе|грудк)",
        13: r"\bрис\b|крупа.*рис", 14: r"филе.*треск|треск.*филе", 15: r"картофель", 16: r"лимон", 17: r"яйц[оа].*курин|курин.*яйц[оа]",
        18: r"брокколи", 19: r"\bнут\b", 20: r"огур", 21: r"\bтворог\b", 22: r"банан", 23: r"йогурт", 24: r"макарон|спагетти|пенне|фарфалле|фузилли|вермишель|паста.*(?:barilla|de cecco)",
        25: r"фасоль", 26: r"(?:филе|грудк).*(?:индей|индюш)|(?:индей|индюш).*(?:филе|грудк)", 27: r"капуста.*белокочан|белокочан.*капуста", 28: r"тыква", 29: r"горошек.*зелен|зелен.*горошек"}
    if ingredient_id not in includes or not re.search(includes[ingredient_id], n):
        return False, 'Название не соответствует ингредиенту'
    prefixes = {1:r'^(?:хлопья|геркулес)', 2:r'^молоко', 4:r'^яблок', 5:r'^(?:крупа|греч|ядрица)', 6:r'^(?:грибы|шампиньон)', 7:r'^лук', 8:r'^(?:масло|оливковое масло)', 9:r'^(?:крупа|чечевица)', 10:r'^морковь', 11:r'^(?:томат|помидор)', 12:r'^(?:филе|грудк|мясо)', 13:r'^(?:рис|крупа)', 14:r'^(?:филе|треска)', 15:r'^картофель', 16:r'^лимон', 17:r'^яйц', 18:r'^(?:брокколи|капуста|овощ)', 19:r'^(?:нут|консерв)', 20:r'^огур', 21:r'^творог', 22:r'^банан', 23:r'^(?:йогурт|биойогурт)', 24:r'^(?:макарон|спагетти|пенне|фарфалле|фузилли|вермишель|паста|изделия макарон)', 25:r'^(?:фасоль|консерв)', 26:r'^(?:филе|грудк|мясо)', 27:r'^капуста', 28:r'^(?:тыква|овощ)', 29:r'^(?:горошек|зеленый горошек|консерв)'}
    if not re.search(prefixes[ingredient_id], n):
        return False, 'Название описывает другой тип товара'
    common = r"суп|салат|пюре|смузи|напиток|сок\b|пирог|пельмен|вареник|пицц|наггетс|котлет|десерт|шоколад|корм\b|чипсы|приправ|сырники|маффин|кекс|булоч|круассан|растительн.*молоко"
    specific = {
        1: r"ассорти|сахар|с (?:молок|яблок|ягод|клубник|малин)|мюсли|гранола|каша",
        2: r"сгущ|кокос|овсян|соев|миндал|сухое|шоколад|топлен|козье|молочко|коктейль",
        4: r"сушен|вялен|пюре|чипс|мармелад|джем|повидл", 5: r"хлопья|готов|варен|с овощ|с гриб",
        6: r"марин|резан.*консерв|солен|консерв", 7: r"сушен|жарен|зелен|порей|марин|хрустящ|чеснок",
        8: r"подсолнеч|кукуруз|рапсов", 9: r"готов|варен|суп|пюре", 10: r"корей|марин|сушен|готов",
        11: r"вялен|сушен|солен|марин|консерв|паста|кетчуп|резан.*сок", 12: r"копчен|варен|марин|запеч|фарш|рулет|панир|с ветчин|стрипс|в соусе",
        13: r"готов|варен|плов|каша|смесь|с овощ|молоч|хлебц|куриц|говяд|свинин|с прян", 14: r"панир|копчен|сушен|солен|в соусе|сыром|креветк|запеч",
        15: r"батат|пюре|готов|варен|фри\b|чипс|по-деревен|дольк", 16: r"сок|сушен|кислота|масло",
        17: r"перепел|варен|сух|меланж", 18: r"смесь|соус|панир", 19: r"сух|крупа|мука|снек|хрустящ",
        20: r"солен|марин|консерв|малосоль", 21: r"масса|творож|сырок|с изюм|с ягод|сахар|с (?:клубник|ванил)",
        22: r"сушен|чипс|вялен|пюре|начинк", 23: r"с (?:ягод|клубник|манго|персик|ванил|черник|злак)|клубнич|персиков|чернич|вишн|малин|банан|виноград|абрикос|малина|питьевой.*фрукт",
        24: r"готов|блюдо|соус|гнезда.*мяс|фарш|лапша.*(?:быстр|курин|говяд)", 25: r"сух|крупа|стручков|лоби|в томат|томатн|с овощ|чили|остр|по-|с гриб",
        26: r"копчен|варен|марин|запеч|фарш|рулет|панир|стейк.*спец|в соусе", 27: r"квашен|солен|марин|морск|салат|пекин|краснокочан|цветн", 28: r"сушен|пюре|сок|семеч|масло|напиток", 29: r"сух|крупа|пюре|суп"}
    if re.search(common, n) or re.search(specific.get(ingredient_id, r"(?!)"), n):
        return False, 'Готовое блюдо, добавки или неподходящая форма ингредиента'
    return True, None


def split_evidence(record, path):
    text = record.get('result')
    if not isinstance(text, str):
        return []
    found = []
    for block in re.split(r"-{40,}", text):
        header = re.search(r"^(.*?)\s+\((https?://[^\s)]+)\)\s*$", block, re.M)
        if not header:
            continue
        title, url = header.groups()
        store = retailer(url)
        if not store or store != record['store']:
            continue
        name = product_name(block, title)
        accepted, reason = matches_ingredient(name, int(record['ingredient_id']))
        comp, comp_complete = extract_composition(block)
        if accepted and comp and record['ingredient_id'] in (19, 25, 29) and not re.search(r'вод[аы]|заливк|рассол|консерв', comp + ' ' + name, re.I):
            accepted, reason = False, 'Сухие бобовые вместо готовых без жидкости'
        if accepted and comp and record['ingredient_id'] == 23 and re.search(r'наполнител|пюре|джем|варень|(?:^|[,;(])\s*сахар(?:[,;)\s]|$)', comp, re.I):
            accepted, reason = False, 'Йогурт с наполнителем или добавленным сахаром'
        nutrition = extract_nutrition(block)
        price, price_unit, regular = extract_price(block, store)
        package = extract_package(name, block)
        if price_unit == 'package' and package['quantity'] is None and re.search(r"(?:за\s*1\s*кг|₽/1?кг)", block[:2000], re.I):
            price_unit = 'kg'
        crawl = re.search(r"Crawled:\s*([^;\n]+)", block)
        flags = []
        if 'Этот товар закончился' in block or re.search(r"Нет в наличии", block[:1500], re.I):
            availability = 'out_of_stock'
            if 'В наличии много' in block:
                flags.append('conflicting_availability')
        elif re.search(r'В корзину|Добавить в корзину|В наличии', block[:2000]):
            availability = 'listed_available'
        else:
            availability = 'unknown'
        if not comp:
            flags.append('missing_composition')
        elif not comp_complete:
            flags.append('truncated_composition')
        if price is None:
            flags.append('missing_price')
        nutrient_fields = ['energy_kcal', 'protein_g', 'fat_g', 'carbohydrate_g']
        complete_nutrition = all(nutrition[k] is not None for k in nutrient_fields) and nutrition['basis_unit'] is not None
        if not complete_nutrition:
            flags.append('incomplete_nutrition')
        elif all(nutrition[k] == 0 for k in nutrient_fields):
            flags.append('zero_nutrition_from_source')
        else:
            energy = nutrition['energy_kcal']
            macro_energy = nutrition['protein_g'] * 4 + nutrition['fat_g'] * 9 + nutrition['carbohydrate_g'] * 4
            if energy > 20 and abs(macro_energy - energy) > max(35, energy * .35):
                flags.append('inconsistent_nutrition_from_source')
        if record['ingredient_id'] in (19, 25, 29) and package['drained_quantity_g'] is None:
            flags.append('drained_weight_not_published')
        raw_context = re.search(r"(?:Москва[^\n]*|Краснодар[^\n]*|Санкт-Петербург[^\n]*|Петербургская улица[^\n]*)", block[:1800])
        product_id = f"{store}:{external_id(url) or hashlib.sha256(canonical_url(url).encode()).hexdigest()[:12]}"
        found.append({
            'product_id': product_id, 'store': store, 'store_name': STORES[store], 'external_id': external_id(url), 'product_name': name,
            'source_url': canonical_url(url), 'original_source_url': url, 'brand': value_after(block, 'Бренд'), 'manufacturer': value_after(block, 'Производитель'),
            'package': package, 'ingredients_text': comp, 'composition_complete': comp_complete, 'nutrition': nutrition, 'nutrition_complete': complete_nutrition,
            'gtin': None, 'image_url': None, 'shelf_life_text': value_after(block, 'Срок хранения'), 'storage_conditions_text': value_after(block, 'Условия хранения'),
            'allergen_warning': (re.search(r'(?:Может содержать|Могут содержаться|Содержит (?:глютен|лактозу|молоко))[^.]*\.?', comp, re.I).group(0) if comp and re.search(r'(?:Может содержать|Могут содержаться|Содержит (?:глютен|лактозу|молоко))[^.]*\.?', comp, re.I) else None),
            'price_rub': price, 'regular_price_rub': regular, 'price_unit': price_unit, 'currency': 'RUB',
            'unit_pricing': unit_pricing(price, price_unit, package),
            'availability': availability, 'price_freshness': 'indexed_unverified', 'region': None, 'retailer_context': raw_context.group(0) if raw_context else None,
            'source_retrieved_at': record['collected_at'], 'indexed_at_label': crawl.group(1).strip() if crawl else None, 'price_observed_at': None,
            'quality_flags': flags, 'ingredient_ids': [record['ingredient_id']] if accepted else [], 'candidate_ingredient_id': record['ingredient_id'],
            'match_status': 'name_matched' if accepted else 'rejected', 'match_rejection_reason': reason,
            'price_observations': [{'price_rub': price, 'regular_price_rub': regular, 'price_unit': price_unit, 'source_url': url, 'source_retrieved_at': record['collected_at'], 'source_index_age': crawl.group(1).strip() if crawl else None, 'region': None, 'retailer_context': raw_context.group(0) if raw_context else None, 'freshness': 'indexed_unverified', 'availability': availability}],
            'evidence': [{'file': str(path.relative_to(path.parent.parent)), 'method': 'official_product_card_index', 'source_url': url, 'sha256': hashlib.sha256(block.encode()).hexdigest()}],
        })
    return found


def build_dataset(out):
    out = Path(out)
    snapshot = json.loads((out / 'recipes.json').read_text())
    ingredients = json.loads((out / 'ingredients.json').read_text())['ingredients']
    by_id = {i['ingredient_id']: i for i in ingredients}
    products, rejected, attempts = {}, [], []
    for path in sorted((out / 'evidence').glob('search_*.json')):
        record = json.loads(path.read_text())
        parsed = split_evidence(record, path)
        attempts.append({k: record.get(k) for k in ('ingredient_id', 'ingredient_name', 'store', 'query', 'pass', 'collected_at', 'error')} | {'source_card_count': len(parsed)})
        for product in parsed:
            if product['match_status'] == 'rejected':
                rejected.append(product)
                continue
            key = product['product_id']
            if key in products:
                current = products[key]
                ingredient_ids = sorted(set(current['ingredient_ids'] + product['ingredient_ids']))
                evidence = current['evidence'] + product['evidence']
                observations = current['price_observations'] + product['price_observations']
                # Keep one coherent snapshot; never assemble a composition and price from different SKUs.
                score = lambda x: (x['composition_complete'], x['nutrition_complete'], x['price_rub'] is not None, len(x['ingredients_text'] or ''))
                if score(product) > score(current):
                    products[key] = product
                products[key]['ingredient_ids'] = ingredient_ids
                products[key]['evidence'] = evidence
                products[key]['price_observations'] = list({json.dumps(o, sort_keys=True): o for o in observations}.values())
            else:
                products[key] = product
    offers, candidates = [], []
    for product in products.values():
        product['recipe_ids'] = sorted({rid for iid in product['ingredient_ids'] for rid in by_id[iid]['recipe_ids']})
        product['nutrition_usable'] = product['nutrition_complete'] and not any(f in product['quality_flags'] for f in ['zero_nutrition_from_source', 'inconsistent_nutrition_from_source'])
        product['eligible_for_offers'] = bool(product['ingredients_text'] and product['composition_complete'])
        (offers if product['eligible_for_offers'] else candidates).append(product)
    coverage = []
    for ingredient in ingredients:
        if not ingredient['purchase_required']:
            continue
        iid = ingredient['ingredient_id']
        for store in STORES:
            matched = [p for p in offers if p['store'] == store and iid in p['ingredient_ids']]
            pending = [p for p in candidates if p['store'] == store and iid in p['ingredient_ids']]
            complete = [p for p in matched if p['nutrition_usable'] and p['price_rub'] is not None]
            coverage.append({'ingredient_id': iid, 'ingredient_name': ingredient['name'], 'store': store, 'offers': len(matched), 'with_price': sum(p['price_rub'] is not None for p in matched), 'with_complete_nutrition': sum(p['nutrition_complete'] for p in matched), 'with_usable_nutrition': sum(p['nutrition_usable'] for p in matched), 'with_composition_price_nutrition': len(complete), 'pending_composition': len(pending), 'coverage_complete': False, 'status': 'collected' if complete else 'partial' if matched else 'missing'})
    compact_recipes = []
    for r in snapshot['recipes']:
        item = {k: r.get(k) for k in ['id', 'title', 'description', 'servings', 'minutes', 'steps', 'meal_categories', 'tags', 'categories']}
        item['nutrition_from_app'] = r.get('nutrition')
        item['nutrition_coverage_from_app'] = r.get('nutrition_coverage')
        item['nutrition_recalculated_for_collected_products'] = False
        item['ingredients'] = [{k: ing.get(k) for k in ('ingredient_id', 'name', 'quantity', 'unit', 'purchase_required', 'substitutions')} | {'product_ids': [p['product_id'] for p in offers if ing['ingredient_id'] in p['ingredient_ids']]} for ing in r['ingredients']]
        item['product_coverage'] = {}
        for store in [*STORES, 'any_store']:
            requirements = [i for i in r['ingredients'] if i.get('purchase_required', True)]
            available = [p for p in offers if store == 'any_store' or p['store'] == store]
            missing = [i['ingredient_id'] for i in requirements if not any(i['ingredient_id'] in p['ingredient_ids'] for p in available)]
            item['product_coverage'][store] = {'all_ingredients_have_product': not missing, 'missing_ingredient_ids': missing}
        compact_recipes.append(item)
    result = {'schema_version': '2.0', 'dataset_id': out.name, 'generated_at': timestamp(), 'source_application': snapshot['source_url'], 'requested_stores': list(STORES), 'recipe_count': len(compact_recipes), 'ingredient_count': len(ingredients), 'purchase_ingredient_count': sum(i['purchase_required'] for i in ingredients), 'offer_count': len(offers), 'candidate_count': len(candidates), 'all_possible_products_collected': False, 'coverage_note': 'Официальные карточки из поискового индекса; полный и актуальный ассортимент по адресу не подтверждён.', 'offers': offers, 'candidates': candidates, 'coverage': coverage, 'recipes': compact_recipes, 'ingredients': ingredients}
    write_json(out / 'catalog.json', result)
    (out / 'catalog.json.gz').write_bytes(gzip.compress((out / 'catalog.json').read_bytes(), mtime=0))
    write_json(out / 'coverage.json', {'generated_at': result['generated_at'], 'coverage': coverage})
    write_json(out / 'rejected_products.json', {'count': len(rejected), 'products': rejected})
    write_json(out / 'search_attempts.json', {'count': len(attempts), 'attempts': attempts})
    with (out / 'products.jsonl').open('w', encoding='utf-8') as handle:
        for product in offers + candidates:
            handle.write(json.dumps(product, ensure_ascii=False) + '\n')
    build_sqlite(out / 'catalog.sqlite3', result)
    summary = {'recipes': len(compact_recipes), 'ingredients': len(ingredients), 'recipe_ingredient_rows': sum(len(r['ingredients']) for r in compact_recipes), 'offers': len(offers), 'candidates': len(candidates), 'by_store': dict(Counter(p['store'] for p in offers)), 'with_price': sum(p['price_rub'] is not None for p in offers), 'complete_composition_price_nutrition': sum(p['nutrition_usable'] and p['price_rub'] is not None for p in offers), 'recipes_with_all_ingredients_any_store': sum(r['product_coverage']['any_store']['all_ingredients_have_product'] for r in compact_recipes), 'coverage_missing': [c for c in coverage if c['status'] == 'missing'], 'quality_flags': dict(Counter(f for p in offers for f in p['quality_flags']))}
    write_json(out / 'summary.json', summary)
    return summary


def build_sqlite(path, dataset):
    path = Path(path)
    temp = path.with_suffix('.tmp.sqlite3')
    if temp.exists():
        temp.unlink()
    with sqlite3.connect(temp) as db:
        db.executescript('''
        CREATE TABLE recipes (id INTEGER PRIMARY KEY, title TEXT NOT NULL, servings INTEGER NOT NULL, data_json TEXT NOT NULL);
        CREATE TABLE ingredients (id INTEGER PRIMARY KEY, name TEXT NOT NULL, purchase_required INTEGER NOT NULL, data_json TEXT NOT NULL);
        CREATE TABLE recipe_ingredients (recipe_id INTEGER NOT NULL REFERENCES recipes(id), ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), quantity TEXT, unit TEXT, PRIMARY KEY(recipe_id, ingredient_id));
        CREATE TABLE products (id TEXT PRIMARY KEY, store TEXT NOT NULL, external_id TEXT, name TEXT NOT NULL, ingredients_text TEXT, price_rub REAL, energy_kcal REAL, protein_g REAL, fat_g REAL, carbohydrate_g REAL, eligible_for_offers INTEGER NOT NULL, source_url TEXT NOT NULL, data_json TEXT NOT NULL);
        CREATE TABLE ingredient_products (ingredient_id INTEGER NOT NULL REFERENCES ingredients(id), product_id TEXT NOT NULL REFERENCES products(id), PRIMARY KEY(ingredient_id, product_id));
        CREATE INDEX products_by_store ON products(store);
        ''')
        for r in dataset['recipes']:
            db.execute('INSERT INTO recipes VALUES (?,?,?,?)', (r['id'], r['title'], r['servings'], json.dumps(r, ensure_ascii=False)))
            for i in r['ingredients']:
                db.execute('INSERT INTO recipe_ingredients VALUES (?,?,?,?)', (r['id'], i['ingredient_id'], i['quantity'], i['unit']))
        for i in dataset['ingredients']:
            db.execute('INSERT INTO ingredients VALUES (?,?,?,?)', (i['ingredient_id'], i['name'], i['purchase_required'], json.dumps(i, ensure_ascii=False)))
        for p in dataset['offers'] + dataset['candidates']:
            n = p['nutrition']
            db.execute('INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)', (p['product_id'], p['store'], p['external_id'], p['product_name'], p['ingredients_text'], p['price_rub'], n['energy_kcal'], n['protein_g'], n['fat_g'], n['carbohydrate_g'], p['eligible_for_offers'], p['source_url'], json.dumps(p, ensure_ascii=False)))
            for iid in p['ingredient_ids']:
                db.execute('INSERT INTO ingredient_products VALUES (?,?)', (iid, p['product_id']))
    temp.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('export-app')
    export.add_argument('--base-url', default='http://127.0.0.1:18081')
    export.add_argument('--out', type=Path, required=True)
    build = sub.add_parser('build')
    build.add_argument('--data', type=Path, required=True)
    args = parser.parse_args()
    result = export_app(args.base_url, args.out) if args.command == 'export-app' else build_dataset(args.data)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
