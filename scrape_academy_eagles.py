import argparse
import html
import json
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, Iterable, List


BASE_URL = "https://academy-eagles.ru"
USER_AGENT = "EaglesKnowledgeBaseBuilder/1.0"
WP_TYPES = (
    "pages",
    "trainer",
    "event",
    "student_achievement",
    "hostel_album",
    "subscription",
)
EXCLUDED_SLUGS = {
    "sample-page",
    "test",
    "тест",
    "тестовый-абонемент",
    "технические-работы",
    "cart",
    "checkout",
    "my-account",
}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored_depth += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr", "div"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


class MainTextExtractor(HTMLParser):
    """Извлекает видимый текст только из основного содержимого страницы."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []
        self.main_depth = 0
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple]) -> None:
        if tag == "main":
            self.main_depth += 1
            return
        if not self.main_depth:
            return
        if tag in {"script", "style", "noscript", "svg"}:
            self.ignored_depth += 1
        elif tag in {"p", "br", "li", "h1", "h2", "h3", "h4", "tr", "div", "section"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)
            return
        if not self.main_depth:
            return
        if tag in {"script", "style", "noscript", "svg"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"p", "li", "h1", "h2", "h3", "h4", "tr", "div", "section"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.main_depth and not self.ignored_depth:
            self.parts.append(data)


def html_to_text(value: str) -> str:
    parser = TextExtractor()
    parser.feed(value or "")
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def fetch_page_text(url: str) -> str:
    body = read_url(url).decode("utf-8", errors="replace")
    parser = MainTextExtractor()
    parser.feed(body)
    text = html.unescape("".join(parser.parts)).replace("\xa0", " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(dict.fromkeys(line for line in lines if line))


def read_url(url: str, attempts: int = 3) -> bytes:
    last_error: Exception = RuntimeError("URL was not requested")
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except Exception as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(attempt + 1)
    raise last_error


def fetch_json(path: str, query: Dict[str, str] = None) -> Any:
    url = f"{BASE_URL}{path}"
    if query:
        url = f"{url}?{urllib.parse.urlencode(query)}"
    return json.loads(read_url(url).decode("utf-8"))


def is_excluded(slug: str) -> bool:
    decoded = urllib.parse.unquote(slug).strip("/").lower()
    return decoded in EXCLUDED_SLUGS


def select_public_meta(item: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for container_name in ("acf", "meta"):
        container = item.get(container_name)
        if not isinstance(container, dict):
            continue
        for key, value in container.items():
            if value not in (None, "", [], {}):
                result[key] = value
    return result


def parse_wp_items(rest_base: str) -> List[Dict[str, Any]]:
    items = fetch_json(
        f"/wp-json/wp/v2/{rest_base}",
        {"per_page": "100", "context": "view"},
    )
    result = []
    for item in items:
        slug = str(item.get("slug") or "")
        if is_excluded(slug):
            continue
        url = item.get("link")
        result.append(
            {
                "id": item.get("id"),
                "slug": slug,
                "title": html_to_text(item.get("title", {}).get("rendered", "")),
                "url": url,
                "modified": item.get("modified"),
                "content": html_to_text(item.get("content", {}).get("rendered", "")),
                "excerpt": html_to_text(item.get("excerpt", {}).get("rendered", "")),
                "meta": select_public_meta(item),
                "page_text": fetch_page_text(url) if url else "",
            }
        )
    return result


def money_value(prices: Dict[str, Any], key: str) -> Any:
    raw = prices.get(key)
    if raw in (None, ""):
        return None
    minor = int(prices.get("currency_minor_unit", 2))
    return int(raw) / (10**minor)


def parse_products() -> List[Dict[str, Any]]:
    items = fetch_json("/wp-json/wc/store/v1/products", {"per_page": "100"})
    result = []
    for item in items:
        slug = str(item.get("slug") or "")
        if is_excluded(slug):
            continue
        prices = item.get("prices") or {}
        url = item.get("permalink")
        result.append(
            {
                "id": item.get("id"),
                "slug": slug,
                "name": item.get("name"),
                "url": url,
                "description": html_to_text(item.get("description", "")),
                "short_description": html_to_text(item.get("short_description", "")),
                "price": money_value(prices, "price"),
                "regular_price": money_value(prices, "regular_price"),
                "sale_price": money_value(prices, "sale_price"),
                "currency": prices.get("currency_code"),
                "categories": [category.get("name") for category in item.get("categories", [])],
                "attributes": item.get("attributes", []),
                "is_purchasable": item.get("is_purchasable"),
                "page_text": fetch_page_text(url) if url else "",
            }
        )
    return result


def build_snapshot() -> Dict[str, Any]:
    sections = {rest_base: parse_wp_items(rest_base) for rest_base in WP_TYPES}
    sections["products"] = parse_products()
    return {
        "source": BASE_URL,
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "homepage_text": fetch_page_text(f"{BASE_URL}/"),
        "sections": sections,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Снимок публичных данных Academy Eagles")
    parser.add_argument("--output", type=Path, help="Записать JSON в указанный файл")
    args = parser.parse_args()

    snapshot = build_snapshot()
    rendered = json.dumps(snapshot, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"snapshot={args.output}")
    else:
        print(rendered)


if __name__ == "__main__":
    main()
