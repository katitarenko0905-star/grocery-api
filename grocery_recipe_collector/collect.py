#!/usr/bin/env python3
"""Collect candidate product offers for a recipe from three grocery websites.

This is a conservative starter scraper: it follows public search/catalog pages and
records only fields visible on the page. Site markup and anti-bot rules change;
verify availability and prices in the store's own checkout flow.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

STORE_NAMES = {"perekrestok": "Перекрёсток", "pyaterochka": "Пятёрочка", "magnit": "Магнит"}
SEARCH_URLS = {
    "perekrestok": "https://www.perekrestok.ru/cat/search?search={query}",
    "pyaterochka": "https://5ka.ru/search/?q={query}",
    "magnit": "https://magnit.ru/search/?q={query}",
}
PRICE_RE = re.compile(r"(?<!\d)(\d[\d\s\u00a0]*(?:[,.]\d{1,2})?)\s*₽")
NUTRITION_PATTERNS = {
    "calories_kcal_per_100": r"(?:Калории|Ккал)\s*([\d,.]+)",
    "protein_g_per_100": r"Белки\s*([\d,.]+)\s*г",
    "fat_g_per_100": r"Жиры\s*([\d,.]+)\s*г",
    "carbohydrate_g_per_100": r"Углеводы\s*([\d,.]+)\s*г",
}
INGREDIENTS_RE = re.compile(
    r"(?:^|\n)\s*(?:Состав(?: продукта)?|Ингредиенты)\s*:?\s*\n?"
    r"([\s\S]*?)(?=\n\s*(?:Пищевая ценность|Описание|Информация|Характеристики|Условия хранения|"
    r"Рекомендации|Отзывы|Наличие|Основные ингредиенты|Другие товары)\b|$)",
    re.IGNORECASE,
)


def number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value.replace("\u00a0", "").replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def normalize_unit(unit: str) -> str | None:
    return {"г": "g", "кг": "kg", "мл": "ml", "л": "l", "шт": "pcs"}.get(unit.lower())


def parse_quantity(text: str) -> tuple[float | None, str | None]:
    match = re.search(r"([\d,.]+)\s*(кг|г|мл|л|шт)", text, re.I)
    return (number(match.group(1)), normalize_unit(match.group(2))) if match else (None, None)


def extract_composition(text: str) -> str | None:
    match = INGREDIENTS_RE.search(text)
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip(" :;\n\t")
    return value if len(value) >= 2 else None


def extract_nutrition(text: str) -> dict[str, float | None]:
    result = {}
    for field, pattern in NUTRITION_PATTERNS.items():
        match = re.search(pattern, text, re.IGNORECASE)
        result[field] = number(match.group(1)) if match else None
    return result


def get_browser():
    """Prefer Camoufox; fall back to stock Playwright Firefox when unavailable."""
    try:
        from camoufox.sync_api import Camoufox

        manager = Camoufox(headless=True)
        browser = manager.__enter__()
        return manager, browser
    except Exception:
        from playwright.sync_api import sync_playwright

        manager = sync_playwright().start()
        browser = manager.firefox.launch(headless=True)
        return manager, browser


def close_browser(manager: Any, browser: Any) -> None:
    try:
        browser.close()
    except Exception:
        pass
    try:
        if hasattr(manager, "__exit__"):
            manager.__exit__(None, None, None)
        elif hasattr(manager, "stop"):
            manager.stop()
    except Exception:
        pass


def search_one(page: Any, store: str, query: str, ingredient: dict[str, Any], region: str | None, max_products: int = 40):
    url = SEARCH_URLS[store].format(query=quote_plus(query))
    response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(1800)
    body = page.locator("body").inner_text(timeout=10000)
    lower = body.lower()
    if any(marker in lower for marker in ("проблемы со связью", "доступ ограничен", "access denied", "captcha")):
        return [], {"status": "blocked", "url": page.url, "message": "Страница вернула блокировку/проверку доступа"}, []
    if len(body.strip()) < 40:
        return [], {"status": "empty", "url": page.url, "http_status": getattr(response, "status", None)}, []

    offers = []
    rejected = []
    # Result cards are collected by links leading to product detail pages. Keep the
    # extraction bounded and deduplicate by URL; markup is intentionally not guessed.
    links = page.locator("a[href]")
    seen = set()
    product_urls = []
    for i in range(min(links.count(), 350)):
        link = links.nth(i)
        href = link.get_attribute("href") or ""
        label = " ".join((link.inner_text() or "").split())
        if store == "perekrestok":
            is_product = "/cat/" in href and "/p/" in href
        elif store == "pyaterochka":
            is_product = "/product/" in href
        else:
            is_product = "/product/" in href
        if not is_product or not label or len(label) < 5:
            continue
        full_url = href if href.startswith("http") else ("https://www.perekrestok.ru" if store == "perekrestok" else "https://5ka.ru" if store == "pyaterochka" else "https://magnit.ru") + href
        if full_url in seen:
            continue
        seen.add(full_url)
        card_text = label
        try:
            card_text = " ".join(link.locator("xpath=ancestor::*[self::article or @data-testid][1]").inner_text(timeout=1000).split()) or label
        except Exception:
            pass
        match_text = f"{label} {card_text}".lower()
        if any(term.lower() in match_text for term in ingredient.get("exclude_terms", [])):
            continue
        product_urls.append((full_url, label, card_text))
        if len(product_urls) >= max_products:
            break

    for full_url, label, card_text in product_urls:
        try:
            detail_response = page.goto(full_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)
            detail_text = page.locator("body").inner_text(timeout=10000)
        except Exception as exc:
            rejected.append({"product_name": label, "source_url": full_url, "reason": f"Не удалось открыть карточку: {str(exc)[:180]}"})
            continue
        composition = extract_composition(detail_text)
        if not composition:
            rejected.append({"product_name": label, "source_url": full_url, "reason": "В карточке не найден состав; товар не включён в offers"})
            continue
        price_match = PRICE_RE.search(detail_text)
        price = number(price_match.group(1)) if price_match else None
        quantity, unit = parse_quantity(detail_text) if parse_quantity(detail_text)[0] is not None else parse_quantity(card_text)
        nutrition = extract_nutrition(detail_text)
        offer = {
            "store": store,
            "store_name": STORE_NAMES[store],
            "region": region,
            "ingredient_name": ingredient["name"],
            "product_name": label,
            "brand": None,
            "package_quantity": quantity,
            "package_unit": unit,
            "price_rub": price,
            "price_per_base_unit_rub": None,
            "recipe_amount": ingredient.get("amount"),
            "recipe_unit": ingredient.get("unit"),
            "recipe_cost_estimate_rub": None,
            **nutrition,
            "ingredients_text": composition,
            "availability": None,
            "match_confidence": 0.55,
            "match_notes": "Состав извлечён из карточки; вручную подтвердите соответствие рецепту и актуальную цену.",
            "source_url": full_url,
            "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        offers.append(offer)
    return offers, {"status": "ok" if offers else "no_complete_products", "url": url, "http_status": getattr(response, "status", None), "offers": len(offers), "rejected_missing_composition": len(rejected)}, rejected


def collect_recipe(recipe: dict[str, Any], region: str | None = None, stores: list[str] | None = None, max_queries_per_ingredient: int = 3):
    stores = stores or recipe.get("stores") or list(STORE_NAMES)
    manager, browser = get_browser()
    offers: list[dict[str, Any]] = []
    rejected_products: list[dict[str, Any]] = []
    statuses: dict[str, Any] = {}
    try:
        # Camoufox's context manager returns a BrowserContext, while the
        # Playwright fallback returns a Browser. Normalize both APIs here.
        context = browser if hasattr(browser, "cookies") and not hasattr(browser, "new_context") else browser.new_context(locale="ru-RU")
        for store in stores:
            if store not in STORE_NAMES:
                statuses[store] = {"status": "unsupported_store"}
                continue
            page = context.new_page()
            statuses[store] = {"status": "running", "ingredients": {}}
            for ingredient in recipe.get("ingredients", []):
                if ingredient["name"].lower() == "вода":
                    statuses[store]["ingredients"][ingredient["name"]] = {"status": "skipped_free_ingredient"}
                    continue
                phrases = ingredient.get("search_terms") or [ingredient["name"]]
                got = []
                result_status = None
                for phrase in phrases[:max_queries_per_ingredient]:
                    try:
                        batch, result_status, rejected = search_one(page, store, phrase, ingredient, region)
                    except Exception as exc:
                        result_status = {"status": "error", "message": str(exc)[:300]}
                        batch = []
                        rejected = []
                    got.extend(batch)
                    rejected_products.extend({"store": store, "ingredient_name": ingredient["name"], **item} for item in rejected)
                    if result_status.get("status") == "blocked":
                        break
                dedup = {o["source_url"]: o for o in got}
                offers.extend(dedup.values())
                statuses[store]["ingredients"][ingredient["name"]] = {**(result_status or {"status":"no_results"}), "offers": len(dedup)}
            page.close()
            vals = [x.get("status") for x in statuses[store]["ingredients"].values()]
            statuses[store]["status"] = "blocked" if "blocked" in vals else "partial" if any(v != "ok" for v in vals) else "ok"
        context.close()
    finally:
        close_browser(manager, browser)
    return {"schema_version": "1.1", "recipe_id": recipe.get("recipe_id"), "recipe_name": recipe.get("name"), "servings": recipe.get("servings"), "region": region, "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"), "platform_status": statuses, "offers": offers, "rejected_products": rejected_products}


def write_csv(path: Path, result: dict[str, Any]):
    fields = ["store", "store_name", "region", "ingredient_name", "product_name", "brand", "package_quantity", "package_unit", "price_rub", "price_per_base_unit_rub", "recipe_amount", "recipe_unit", "recipe_cost_estimate_rub", "calories_kcal_per_100", "protein_g_per_100", "fat_g_per_100", "carbohydrate_g_per_100", "ingredients_text", "availability", "match_confidence", "match_notes", "source_url", "collected_at"]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(result["offers"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recipe", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--region", default=None, help="Регион/город для метаданных; цены и магазин на сайтах задаются их собственным интерфейсом")
    parser.add_argument("--stores", nargs="+", choices=list(STORE_NAMES), default=None)
    parser.add_argument("--max-queries-per-ingredient", type=int, default=3)
    args = parser.parse_args()
    recipe = json.loads(args.recipe.read_text(encoding="utf-8"))
    result = collect_recipe(recipe, args.region, args.stores, args.max_queries_per_ingredient)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        write_csv(args.csv, result)
    print(json.dumps({"out": str(args.out), "offers": len(result["offers"]), "platform_status": result["platform_status"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
